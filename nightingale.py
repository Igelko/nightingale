#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import shutil
import logging
import argparse
import tempfile
import subprocess

from itertools import chain, product
from contextlib import contextmanager
from datetime import datetime, timedelta

from smtplib import SMTP_SSL as SMTP
from email.mime.text import MIMEText
from email.header import Header

from jinja2 import Environment, FileSystemLoader


@contextmanager
def tempdir(delete=False):
    try:
        path = tempfile.mkdtemp(prefix='nightingale-')
        print('tempdir: ', path)
        yield path
    finally:
        if delete:
            shutil.rmtree(path)

@contextmanager
def inside_path(new_dir):
    old_dir = os.getcwd()
    try:
        os.chdir(new_dir)
        yield
    finally:
        os.chdir(old_dir)

@contextmanager
def docker_container(image):
    container = subprocess.check_output(['docker', 'create', image]).decode('utf-8').strip()
    try:
        yield container
    finally:
        subprocess.check_call(['docker', 'rm', container])

def make_version(path, app):
    version = None
    with inside_path(path):
        # try to get latest tag from git
        try:
                version = subprocess.check_output(['git', 'describe', '--abbrev=0', '--tags']).decode('utf-8').strip()
        except subprocess.SubprocessError as e:
            pass

        # get from config file
        if version == None:
            version = app.get('version', '0.0.1')

        # add version postfix for nightly builds
        if app.get('mode', None) == 'nightly':
            version += datetime.now().strftime('-%Y%m%d%H%M')
            set_version_cmd = app.get('version_cmd', None);
            if set_version_cmd:
                subprocess.check_call(set_version_cmd.format(version=version), shell=True)

    return version

def execute_custom_prebuid_cmd(prefix, image_name, path, app):
    with inside_path(path):
        subprocess.check_call(app['buildcmd'], shell=True)
        appdir = image_name + '_build'
        shutil.copytree(os.path.join(path, app['builddir']), os.path.join(prefix, appdir))
    shutil.rmtree(path)
    return appdir

def repack_docker_image(image_id, image_name):
    with docker_container(image_id) as container:
        tmp_image_id = image_name + ':flat'
        subprocess.check_call(''.join(['docker export ', container,
            ' | docker import - ', tmp_image_id]), shell=True)
    return tmp_image_id

def save_and_clean_docker_image(tag, imagedir):
    subprocess.check_call(''.join(['docker save ', tag,
        ' | xz --compress -9 --threads=0 > ', os.path.join(imagedir, tag.replace(':', '_') + '.tar.xz')
        ]), shell=True)

def push_docker_image(tag, registry_url):
    remote_tag = '/'.join([registry_url, tag])
    subprocess.check_call(['docker', 'tag', tag, remote_tag])
    subprocess.check_call(['docker', 'push', remote_tag])

def build(prefix, templates, app, verbose=False):
    image_name = app['name']
    path = os.path.join(prefix, image_name)

    # clone repository
    subprocess.check_call([
        'git', 'clone',
        '--branch', app['branch'],
        # '--depth', '1',
        app['repo'], path
    ])

    subdir = app.get('subdir', None)
    if subdir:
        path = os.path.join(path, subdir)

    version = make_version(path, app)

    # install dependencies and build release files
    if 'buildcmd' in app:
        appdir = execute_custom_prebuid_cmd(prefix, image_name, path, app)
    else:
        appdir = os.path.relpath(path, prefix)

    # prepare docker template
    tmpl = templates.get_template(app['docker_template'] + '.j2')
    dockerfile_name = os.path.join(prefix, image_name + '.Dockerfile')
    with open(dockerfile_name, 'w') as dockerfile:
        dockerfile.write(tmpl.render(appdir=appdir))

    # build image
    with inside_path(prefix):
        tmp_image_id = image_name + ':tmp'
        if verbose:
            subprocess.check_call(['docker', 'build', '-t', tmp_image_id, '--file', dockerfile_name, '.'])
        else:
            subprocess.check_call(['docker', 'build', '--quiet', '-t', tmp_image_id, '--file', dockerfile_name, '.'])

    # repack and replace release image
    if app['mode'] == 'release':
        flat_image_id = repack_docker_image(tmp_image_id, image_name)
        subprocess.check_call(['docker', 'rmi', tmp_image_id])
        tmp_image_id = flat_image_id

    # postbuild
    tmpl = templates.get_template('postbuild.j2')
    dockerfile_name = os.path.join(prefix, image_name + '.postbuild.Dockerfile')
    with open(dockerfile_name, 'w') as dockerfile:
        dockerfile.write(tmpl.render(imagename=tmp_image_id, appname=app['name']))
    tag = image_name + ':' + version
    with inside_path(prefix):
        subprocess.check_call(['docker', 'build', '-t', tag, '--file', dockerfile_name, '.'])

    # remove temp tag
    subprocess.check_call(['docker', 'rmi', tmp_image_id])

    return tag


def docker_ps():
    class Container:
        def __init__(self, id, image, port_forward, status, *args):
            self.id = id
            tmp = image.split(':')
            self.image = tmp[0]
            self.tag = tmp[1] if len(tmp) == 2 else None
            self.host, self.port = \
                re.match('(?:(?P<host>[\w\.]+):(?P<port>\d+)->)?\d+/\w+', port_forward).groups() \
                    if port_forward else (None, None)

            self.status = status

        def match(self, image_name, port):
            if self.image != image_name:
                return False
            if self.port:
                return self.port == port
            return True

    cont_out = subprocess.check_output(['docker', 'ps', '-a', '--format', '{{ .ID }} {{ .Image }} {{ .Ports }} {{ .Status }}' ]).decode('utf-8')
    return [Container(*line.split(' ', 4)) for line in cont_out.split('\n') if line]


def docker_images():
    class Image:
        def __init__(self, line):
            self.name, self.tag, self.id, _etc = re.split('\s+', line, 3)
            date_candidate = re.match(r'.*(-\d{12})', self.tag)
            self.date = datetime.strptime(date_candidate.groups()[0], '-%Y%m%d%H%M') if date_candidate else None

        def __repr__(self):
            return '%s %s:%s' % (self.id, self.name, self.tag)

    cont_out = subprocess.check_output(['docker', 'images']).decode('utf-8')
    return [Image(line) for line in cont_out.split('\n') if line]


def run(config, image_id, app):
    containers = docker_ps()
    image_name, image_tag = image_id.split(':')

    # remove old similar containers
    for container in containers:
        if container.match(image_name, app.get('port', None)):
            subprocess.check_call(['docker', 'stop', container.id])
            subprocess.check_call(['docker', 'rm', container.id])

    command = ['docker', 'run', '-d', '--restart=always', '--name', image_name, '--dns=' + config['dns']]

    if 'port' in app:
        ports = ['-p', '0.0.0.0:' + app['port'] + ':' + app['inner_port'], '--expose=' + app['inner_port']]
        command.extend(ports)

    env = chain(*product(['-e'], ["{}={}".format(*item) for item in app.get('envvars', {}).items()]))
    command.extend(env)

    volumes = chain(*product(['-v'], ['/var/log/' + image_name + ':/var/log:rw'] + app.get('volumes', [])))
    command.extend(volumes)

    command.append(image_id)

    subprocess.check_call(command)


def rotate(max_days):
    images = docker_images()
    containers = set((container.image, container.tag) for container in docker_ps())
    for image in images:
        if image.date and ((datetime.now() - image.date) > timedelta(days=max_days)):
            if (image.name, image.tag) in containers:
                print('WARNING: Running container on obsolete image:', image)
            else:
                subprocess.check_call(['docker', 'rmi', image.name + ':' + image.tag])


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', dest='config', help='configuration JSON-file')
    parser.add_argument('--envdir', dest='envdir',
        default='./environment', help='additional environment for docker build')
    parser.add_argument('--templatedir', dest='templates',
        default='./templates', help='templates directory for dockerfiles')
    parser.add_argument('--tries', metavar='R', dest='tries',
        default=1, type=int, help='max tries of build')
    parser.add_argument('--retries-delay', metavar='D', dest='retries_delay',
        default=0, type=int, help='delay in seconds between try loops')
    parser.add_argument('--savetmp', dest='deletetemp',
        default=True, action='store_false', help='Save temporary directory')
    parser.add_argument('--verbose', dest='verbose',
        default=False, action='store_true', help='Print logs from docker build')
    parser.add_argument('--send-mail', dest='send_mail',
        default=False, action='store_true', help='Send report mail after build')
    parser.add_argument('--build', dest='build',
        default=False, action='store_true', help='Build and run new images')
    parser.add_argument('--rotate', metavar='N', dest='max_days',
        default=False, type=int, help='Rotate images older than N days')
    parser.add_argument('--imagedir', dest='imagedir', default='.', help='path to save docker images')
    parser.add_argument('--registry', dest='registries', action='append', default=[], help='registry url')
    parser.add_argument('applications', nargs='*', help='Limit applications from config to build')
    return parser.parse_args()


def get_config(path):
    with open(path, 'r') as data:
        return json.load(data)


def make_a_try(temp, templates, app, config, options):
    d1 = datetime.now()
    try:
        print('Build %s' % app['name'])
        image_id = build(temp, templates, app, options.verbose)

        # save release image
        if options.imagedir != '.':
            save_and_clean_docker_image(image_id, options.imagedir)

        # push image to registries if exists
        if options.registries:
            for registry in options.registries:
                push_docker_image(image_id, registry)

        if app['mode'] == 'nightly':
            run(config, image_id, app)

        success = True
        message = image_id.split(':')[1]
    except Exception as e:
        print('!!!!!!!!!!!!!!!!!!!! FAIL! !!!!!!!!!!!!!!!!!!!!!!')
        print(app['name'])
        print('Error %s' % e)
        success = False
        message = 'Ошибка сборки!'
    d2 = datetime.now()
    return { "success": success, "app": app['name'], "message": message, "build_time": str(d2 - d1) }


def process_builds(apps, config, options):
    build_results = []
    failed_apps = []
    templates = Environment(loader=FileSystemLoader(options.templates))
    with tempdir(delete=options.deletetemp) as temp:
        # copy context
        shutil.copytree(options.envdir, os.path.join(temp, 'environment'))
        if options.build:
            for app in apps:
                build_result = make_a_try(temp, templates, app, config, options)
                build_results.append(build_result)
                if not build_result['success']:
                    failed_apps.append(app)

        if options.max_days:
            rotate(options.max_days)

    return build_results, failed_apps


def send_mail(host, port, user, passwd, fromaddr, toaddrs, subject, message, encoding='utf-8'):
    try:
        msg = MIMEText(message, 'plain', encoding)
        msg['From'] = Header(fromaddr)
        msg['Subject'] = Header(subject, encoding)
        #sends mail.
        smtp = SMTP(host, port)
        #smtp.set_debuglevel(True)
        if user and passwd:
            smtp.login(user, passwd)
        smtp.sendmail(fromaddr, toaddrs, msg.as_string())
        smtp.quit()
        print('Mail sent successfuly')
    except Exception as e:
        print('Error on mail senfing! %s' % repr(e))


def compose_mail(build_results):
    build_status = 'OK' if all(result['success'] for result in build_results) else 'FAIL'
    subject = datetime.now().strftime('Nightlty build at %Y-%m-%d %H:%M. {}'.format(build_status))
    message = '\n'.join('%s - %s - Время сборки: %s' % (result['app'], result['message'], result['build_time']) for result in build_results)
    return { 'subject': subject, 'message': message }


def main():
    options = parse_arguments()
    config = get_config(options.config) if options.config else {}
    print(options)
    print(config)
    apps = [ app for app in config['apps'] if app['name'] in options.applications ] \
        if options.applications \
        else config['apps']

    for i in range(options.tries):
        print('Try #%s' % (i + 1))
        build_results, apps = process_builds(apps, config, options)
        if build_results and options.send_mail:
            if 'smtp' not in config:
                raise Exception('Need smtp section in config file!')
            mail = compose_mail(build_results)
            mail.update(config['smtp'])
            send_mail(**mail)

        if not apps:
            break
        time.sleep(options.retries_delay)

if __name__ == '__main__':
    main()

