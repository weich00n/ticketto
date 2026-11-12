# syntax=docker/dockerfile:1
# Ticketto's RabbitMQ broker: the official image plus our configuration, plugins, topology,
# entrypoint and health check.
#
# There are deliberately no RUN steps. That lets `docker buildx` build the arm64 image for
# the EC2 broker host on an amd64 CI runner without CPU emulation.
FROM rabbitmq:4.3.6-management

LABEL org.opencontainers.image.title="ticketto-rabbitmq" \
      org.opencontainers.image.description="Ticketto message broker with its topology preloaded" \
      org.opencontainers.image.source="https://gitlab.com/cs302-2026/g2-team5/backend/rabbitmq-service"

COPY --chmod=0644 config/rabbitmq.conf /etc/rabbitmq/conf.d/20-ticketto.conf
COPY --chmod=0644 config/enabled_plugins /etc/rabbitmq/enabled_plugins
# The entrypoint writes 20-users.json next to this file at start-up. The symbolic mode
# matters: --chmod also applies to the new definitions/ directory, and an octal 0644 would
# leave it without the execute bit, so RabbitMQ could not read the file inside.
COPY --chown=rabbitmq:rabbitmq --chmod=u=rwX,go=rX topology/definitions.json /etc/rabbitmq/definitions/10-topology.json
COPY --chmod=0644 topology/queues.txt /etc/rabbitmq/ticketto-queues.txt
# Service users and their permissions (no passwords): the entrypoint turns them into users.
COPY --chmod=0644 topology/service-users.tsv /etc/rabbitmq/ticketto-service-users.tsv
COPY --chmod=0755 docker-entrypoint-ticketto.sh /usr/local/bin/docker-entrypoint-ticketto.sh
COPY --chmod=0755 scripts/ticketto-check.sh /usr/local/bin/ticketto-check

# Healthy = the node is up and not blocking publishers because of a memory or disk alarm.
HEALTHCHECK --interval=10s --timeout=20s --start-period=60s --retries=6 \
  CMD rabbitmq-diagnostics -q check_running && rabbitmq-diagnostics -q check_local_alarms

ENTRYPOINT ["docker-entrypoint-ticketto.sh"]
CMD ["rabbitmq-server"]
