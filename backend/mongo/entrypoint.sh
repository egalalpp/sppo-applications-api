#!/bin/sh
# Обёртка над стандартной точкой входа образа mongo.
#
# Транзакции MongoDB (статус заявки и запись в audit_logs одним атомарным действием) работают только
# на replica set. Для локального стенда поднимаем replica set из одного узла: mongod запускается с
# --replSet rs0, а при включённой авторизации ему обязателен keyFile — создаём его здесь.
# Для одного узла содержимое ключа ни с чем не согласуется, поэтому он каждый раз новый.
set -e

KEYFILE=/tmp/mongo-keyfile
if [ ! -s "$KEYFILE" ]; then
  head -c 512 /dev/urandom | base64 | tr -d '\n' > "$KEYFILE"
fi
chown mongodb:mongodb "$KEYFILE"
chmod 400 "$KEYFILE"

exec docker-entrypoint.sh "$@"
