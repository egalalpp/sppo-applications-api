#!/bin/sh
# Проверка готовности MongoDB для healthcheck в docker-compose.
#
# Заодно один раз инициализирует replica set (rs.initiate): так не нужно выполнять эту команду вручную.
# Пока набор не создан или узел ещё не стал PRIMARY, скрипт завершается с ошибкой, и compose считает
# контейнер неготовым; после выбора PRIMARY — успех.
exec mongosh --quiet \
  --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin \
  --eval '
    try {
      quit(rs.status().myState === 1 ? 0 : 1);
    } catch (e) {
      // NotYetInitialized (94) — набор ещё не создан: создаём его из одного узла
      rs.initiate({ _id: "rs0", members: [{ _id: 0, host: "mongo:27017" }] });
      quit(1);
    }'
