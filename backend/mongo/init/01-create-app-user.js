// Выполняется официальным образом mongo один раз — при первом запуске на пустом томе.
// Приложение работает не под root, а под отдельным пользователем с правами только на свою БД.
// readWrite — данные и индексы, dbAdmin — collMod для обновления валидаторов $jsonSchema.

const dbName = process.env.MONGO_DB;
const user = process.env.MONGO_APP_USER;
const pwd = process.env.MONGO_APP_PASSWORD;

db.getSiblingDB(dbName).createUser({
  user: user,
  pwd: pwd,
  roles: [
    { role: "readWrite", db: dbName },
    { role: "dbAdmin", db: dbName },
  ],
});

print(`Created application user '${user}' for database '${dbName}'`);
