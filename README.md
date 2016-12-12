# Nightingale - nightly build tool for js projects in docker.

## Dependencies

- python 3.4+
- Docker 1.9+
- python-jinja2

## Usage

    usage: nightingale.py [-h] [--config CONFIG] [--envdir ENVDIR]
                        [--templatedir TEMPLATES] [--tries R]
                        [--retries-delay D] [--savetmp] [--send-mail] [--build]
                        [--rotate N] [--imagedir IMAGEDIR]
                        [applications [applications ...]]

    positional arguments:
    applications          Limit applications from config to build

    optional arguments:
    -h, --help            show this help message and exit
    --config CONFIG       configuration JSON-file
    --envdir ENVDIR       additional environment for docker build
    --templatedir TEMPLATES
                            templates directory for dockerfiles
    --tries R             max tries of build
    --retries-delay D     delay in seconds between try loops
    --savetmp             Save temporary directory
    --send-mail           Send report mail after build
    --build               Build and run new images
    --rotate N            Rotate images older than N days
    --imagedir IMAGEDIR   path to save docker images

Samples:

Do a all builds from config/nightly.json and rotate older 7 days images.

    ./nightingale.py --config ./config/nightly.json --build --rotate 7

Build only a `nodejs_sample` from config/nightly.json.

    ./nightingale.py --config "./config/nightly.json" --build nodejs_sample

Do a build from config/release.json. And save release mode sections to ./images

    ./nightingale.py --config ./config/release.json --imagedir ./images --build

## Config file format

    {
        "smtp": {
            "host": "smtp.example.com",
            "port": 465,
            "user": "builds-mailer@example.com",
            "passwd": "SECRET",
            "fromaddr": "builds-mailer@example.com",
            "toaddrs": ["dev1@example.com", "pm@example.com"]
        },
        "dns": "192.168.111.1",                                             # dns server for docker containers.
        "apps": [                                                           # array of apps for build
            {
                "name": "aks_monitor_web",                                  # docker image name
                "repo": "git@gitlab.com:kconcern/aks-monitor-web.git",      # project repo to build
                "branch": "release",                                        # tag / branch
                "mode": "release",                                          # mode. "nightly" - build and run on current host or "release" - build and save to --imagedir
                "docker_template": "node-oracle",                           # template for build.
                "buildcmd": "npm run build:gulp",                           # additional build command. used for gulp / grunt etc.
                "builddir": "dist"                                          # directory inside repo folder with built artifacts.
                "port": "11345",                                            # External port for listen. Used in nigthly mode.
                "inner_port": "1345"                                        # Port to expose inside docker container. Used in nigthly mode.
                "volumes": [                                                # List of volumes to mount. Used in nigthly mode.
                    "/mnt/storage/builds/sap-exchange:/app/exchange:rw"     # Docker -v argument. `<path on host>:<path inside>:<ro|rw>`
                ],
                "env": {                                                    # Environment variables dictionary for container works for nightly mode.
                    "TZ": "Europe/Samara"
                }
            }
        ]
    }

## Supported templates.

All templates is a Dockerfile jinja2 template.
There is a sample template for nodejs applications in `templates/node.j2`.
Mandatory template for releases is a postbuild.j2. It used for initializing parameters (ENV, RUN, ENTRYPOINT, EXPOSE and so on) for images.


## How it works

1. Create temp directory and copy environment.
2. For each section in `apps`

    1. Clone a repo.
    2. Add a datetime to package version for nightly builds.
    3. Run `npm install && <buildcmd>` if needed.
    4. Generate `<image_name>.Dockerfile` from `docker_template`
    5. Build docker container with repo directory or `builddir`.
    6. Repack image for releases. Do a `docker save | docker load` and erase all variables from Dockerfile.
    7. Generate and run `postbuild.Dockerfile`. It's a same for all projects.
    8. Tag image `<name>_mode:<version>`
    9. Save release image to `--imagedir`

    10. For `"nigthly"` sections stop and remove old container with same `name` and `port` (if present) and run built image.

3. If `--rotate`, remove images older than `max_days`. Date matches from tags and remove.

