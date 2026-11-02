# ProfitSamurai

Клиент — это компания продавца. У каждого клиента своя физическая база Postgres. Служебная база хранит реестр компаний и подключений магазинов.

`POST /clients` создаёт компанию и её базу. `POST /clients/{id}/shops` принимает Api-Key магазина, вызывает `GET /v2/campaigns` и записывает кампании в базу этого клиента. Ключ в реестре лежит в виде шифротекста Fernet.

Как устроена база и зачем каждое поле: [docs/data-model.md](docs/data-model.md).

Почему так, а не иначе: [docs/adr](docs/adr).

## Запуск

```bash
docker compose up --build
```

В PowerShell кавычки внутри `-d` нужно экранировать, иначе `curl.exe` отправит тело без них:

```powershell
curl.exe -s -X POST http://localhost:8000/clients -H "Content-Type: application/json" -H "X-Bootstrap-Token: change-me" -d "{\`"name\`":\`"Магазин\`"}"
```

В ответе будут `id`, `database_name` и `status`. Если статус `failed`, повтор с тем же id:

```powershell
curl.exe -s -X POST http://localhost:8000/clients/ID/provision -H "X-Bootstrap-Token: change-me"
```

Когда статус `ready`, Api-Key записывает кампании и модель размещения в базу клиента:

```powershell
curl.exe -s -X POST http://localhost:8000/clients/ID/shops -H "Content-Type: application/json" -H "X-Bootstrap-Token: change-me" -d "{\`"api_key\`":\`"КЛЮЧ\`"}"
```

Ответ — список кабинетов, которые вернул Маркет:

```json
[{"business_id": 1, "campaign_ids": [10, 11]}]
```
