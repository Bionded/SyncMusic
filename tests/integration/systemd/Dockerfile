# A Debian with systemd as PID 1 for the service install test (test_install_service.py); run --privileged.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv sudo curl ca-certificates && rm -rf /var/lib/apt/lists/* && useradd -m tester
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
