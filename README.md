# ProfitSamurai

Первый срез: один запрос к Partner API Яндекс Маркета и одна служебная база.

`POST /cabinets` принимает Api-Key кабинета, вызывает `GET /v2/campaigns` и записывает только идентификаторы: кабинет (`business.id`) и его кампании (`campaign.id`). Ключ в базе лежит в виде шифротекста Fernet.

Как устроена база и зачем каждое поле: [docs/data-model.md](docs/data-model.md).

Почему так, а не иначе: [docs/adr](docs/adr).

## Запуск

```bash
docker compose up --build
```

```bash
curl -s -X POST http://localhost:8000/cabinets \
  -H "Content-Type: application/json" \
  -H "X-Bootstrap-Token: change-me" \
  -d "{\"api_key\":\"КЛЮЧ\"}"
```

Ответ — список кабинетов, которые вернул Маркет:

```json
[{"business_id": 1, "campaign_ids": [10, 11]}]
```

Повторный вызов с тем же ключом обновляет шифротекст и заменяет набор кампаний тех кабинетов, которые пришли в ответе.

Прочитать уже записанный кабинет:

```bash
curl -s http://localhost:8000/cabinets/1 -H "X-Bootstrap-Token: change-me"
```
