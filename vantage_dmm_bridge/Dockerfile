# Recent Supervisor versions no longer pass BUILD_FROM: plain Alpine by default
ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# py3-pillow: non-square logos are made square before they are sent to the app
RUN apk add --no-cache python3 py3-pillow ca-certificates

COPY dmm_server.py /dmm_server.py

CMD [ "python3", "-u", "/dmm_server.py" ]
