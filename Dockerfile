FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    cups \
    cups-client \
    avahi-daemon \
    avahi-utils \
    dbus \
    ghostscript \
    poppler-utils \
    supervisor \
    python3 \
    python3-pip \
    python3-venv \
    procps \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir flask gunicorn pikepdf pillow numpy

ENV PATH="/opt/venv/bin:$PATH"

# CUPS filters + PPDs for the two Bixolon queues (DHL label, generic scale-to-fit)
COPY cups/filters/_common.py /usr/lib/cups/filter/_common.py
COPY cups/filters/bixolonzpl /usr/lib/cups/filter/bixolonzpl
COPY cups/filters/bixolonplain /usr/lib/cups/filter/bixolonplain
COPY cups/ppd/bixolon-xd5-43t-label.ppd /opt/cups/bixolon-xd5-43t-label.ppd
COPY cups/ppd/bixolon-xd5-43t-plain.ppd /opt/cups/bixolon-xd5-43t-plain.ppd
RUN chmod +x /usr/lib/cups/filter/bixolonzpl /usr/lib/cups/filter/bixolonplain

COPY cups/cupsd.conf /etc/cups/cupsd.conf

# Web app
COPY app /opt/app

# Process supervisor
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

RUN mkdir -p /run/dbus /config /var/log/supervisor

EXPOSE 631 8080

VOLUME ["/config"]

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf", "-n"]
