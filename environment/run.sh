#!/bin/bash

# WARNING: TZ must be set!
if [ -v TZ ] ; then
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone ;
fi

cd /app
npm start

