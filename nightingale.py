#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import shutil
import logging
import argparse
import tempfile
import subprocess

from itertools import chain, product
from contextlib import contextmanager
from datetime import datetime, timedelta

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

def make_version(path, mode):
    with open(os.path.join(path, 'package.json'), 'r+') as package_file:
        package_json = json.load(package_file)

        # add version postfix for nightly builds
        if mode == 'nightly':
            package_json['version'] += datetime.now().strftime('-%Y%m%d%H%M')
            package_file.seek(0)
            package_file.truncate()
            json.dump(package_json, package_file, indent=2)

        return package_json['version']

def execute_custom_prebuid_cmd(prefix, image_name, path, app):
    with inside_path(path):
        subprocess.check_call(['npm', 'install'])
        subprocess.check_call(app['buildcmd'], shell=True)
        shutil.rmtree('node_modules')
        appdir = image_name + '_build'
        shutil.copytree(os.path.join(path, app['builddir']), os.path.join(prefix, appdir))
    return appdir

def repack_docker_image(image_id, image_name):
    with docker_container(image_id) as container:
        tmp_image_id = image_name + ':flat'
        subprocess.check_call(''.join(['docker export ', container,
            ' | docker import - ', tmp_image_id]), shell=True)
    return tmp_image_id

def save_and_clean_docker_image(tag, imagedir):
    subprocess.check_call(''.join(['docker save ', tag,
        ' | xz --compress -9 > ', os.path.join(imagedir, tag.replace(':', '_') + '.tar.xz')
        ]), shell=True)

def build(prefix, templates, imagedir, app):
    if app['mode'] != 'release':
        image_name = app['name'] + '_' + app['mode']
    else:
        image_name = app['name']
    path = os.path.join(prefix, image_name)
    # clone repository
    subprocess.check_call([
        'git', 'clone',
        '--branch', app['branch'],
        '--depth', '1',
        '--recursive',
        app['repo'], path
    ])

    version = make_version(path, app.get('mode', None))

    # install dependencies and build release files
    if 'buildcmd' in app:
        appdir = execute_custom_prebuid_cmd(prefix, image_name, path, app)
    else:
        appdir = image_name

    # prepare docker template
    tmpl = templates.get_template(app['docker_template'] + '.j2')
    dockerfile_name = os.path.join(prefix, image_name + '.Dockerfile')
    with open(dockerfile_name, 'w') as dockerfile:
        dockerfile.write(tmpl.render(appdir=appdir))

    # build image
    with inside_path(prefix):
        tmp_image_id = image_name + ':tmp'
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

    # save release image
    if app['mode'] == 'release':
        save_and_clean_docker_image(tag, imagedir)

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
        if container.image == image_name and ((container.port == app['port']) if container.port else True):
            subprocess.check_call(['docker', 'stop', container.id])
            subprocess.check_call(['docker', 'rm', container.id])

    command = ['docker', 'run', '-d', '--dns=' + config['dns']]

    if 'port' in app:
        ports = ['-p', '0.0.0.0:' + app['port'] + ':' + app['inner_port'], '--expose=' + app['inner_port']]
        command.extend(ports)

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
    parser.add_argument('--savetmp', dest='deletetemp',
        default=True, action='store_false', help='Save temporary directory')
    parser.add_argument('--build', dest='build',
        default=False, action='store_true', help='Build and run new images')
    parser.add_argument('--rotate', metavar='N', dest='max_days',
        default=False, type=int, help='Rotate images older than N days')
    parser.add_argument('--imagedir', dest='imagedir', default=os.getcwd(), help='path to save docker images')
    parser.add_argument('applications', nargs='*', help='Limit applications from config to build')
    return parser.parse_args()


def get_config(path):
    with open(path, 'r') as data:
        return json.load(data)


def main():
    options = parse_arguments()
    config = get_config(options.config) if options.config else {}
    print(options)
    print(config)
    templates = Environment(loader=FileSystemLoader(options.templates))
    with tempdir(delete=options.deletetemp) as temp:
        # copy context
        shutil.copytree(options.envdir, os.path.join(temp, 'environment'))
        if options.build:
            for app in config['apps']:
                try:
                    if not options.applications or app['name'] in options.applications:
                            image_id = build(temp, templates, options.imagedir, app)
                            if app['mode'] == 'nightly':
                                run(config, image_id, app)
                    else:
                        print('Skip ', app['name'])
                except Exception as e:
                    print('!!!!!!!!!!!!!!!!!!!! FAIL! !!!!!!!!!!!!!!!!!!!!!!')
                    print(app['name'])
                    print('Error %s' % e)
                    raise e

        if options.max_days:
            rotate(options.max_days)


if __name__ == '__main__':
    main()
