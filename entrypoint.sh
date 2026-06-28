#!/bin/sh
# تشغيل nginx في الخلفية (بدون PROXY protocol)
nginx -g "daemon off;" &
# تشغيل السكريبت الرئيسي
python3 /manager.py
