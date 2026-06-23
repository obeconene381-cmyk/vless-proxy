FROM alpine:latest

RUN apk add --no-cache nginx bash

# إنشاء المجلدات الضرورية لتفادي أخطاء الصلاحيات فـ جوجل كلاود
RUN mkdir -p /run/nginx /var/log/nginx

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8080

# تشغيل Nginx فـ الواجهة (daemon off) باش الحاوية تبقى حية وما تموتش
CMD ["nginx", "-g", "daemon off;"]
