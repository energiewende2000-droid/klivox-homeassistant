ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 py3-requests py3-cryptography py3-pip jq \
 && (pip3 install --no-cache-dir --break-system-packages zeroconf || true)
COPY klivox-bridge.py /opt/klivox-bridge/klivox-bridge.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
