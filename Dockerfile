FROM nginx:alpine
RUN apk add --no-cache curl bash unzip procps python3 py3-pip
RUN curl -L https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip -o /tmp/xray.zip && \
    mkdir -p /usr/local/bin /usr/local/etc/xray && \
    unzip /tmp/xray.zip -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/xray && \
    rm /tmp/xray.zip
RUN pip3 install --break-system-packages redis
COPY nginx.conf /etc/nginx/nginx.conf
COPY manager.py /manager.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
