#!/bin/sh
nginx -g "daemon off;" &
python3 /manager.py
