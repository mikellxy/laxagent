# laxagent

---

```bash
git clone https://github.com/mikellxy/laxagent.git && cd laxagent
# `deploy/compose/config.json` 配置 `llm`
docker-compose -f deploy/compose/compose.yaml up -d --build
```

访问 `http://yourip:8000` 开始使用 laxagent！

---

建议将 `deploy/compose/config.json` 中的 `$.auth.jwt_secret` 和 `$.database.password` 改为自己的值。

启动后如想再次修改 PG 密码，请删除 PG 容器及其数据卷，再重新运行 Compose 生效（会清空数据库）。
