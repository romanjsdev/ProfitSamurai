# ProfitSamurai

Первый срез: один запрос к Partner API Яндекс Маркета и одна служебная база.

`POST /cabinets` принимает Api-Key кабинета, вызывает `GET /v2/campaigns` и записывает только идентификаторы: кабинет (`business.id`) и его кампании (`campaign.id`). Ключ в базе лежит в виде шифротекста Fernet.

Как устроена база и зачем каждое поле: [docs/data-model.md](docs/data-model.md).

Почему так, а не иначе: [docs/adr](docs/adr).

## Запуск

```bash
docker compose up --build
```

В PowerShell кавычки внутри `-d` нужно экранировать, иначе `curl.exe` отправит тело без них:

```powershell
curl.exe -s -X POST http://localhost:8000/cabinets -H "Content-Type: application/json" -H "X-Bootstrap-Token: change-me" -d "{\`"api_key\`":\`"КЛЮЧ\`"}"
```

Ответ — список кабинетов, которые вернул Маркет:

```json
[{"business_id": 1, "campaign_ids": [10, 11]}]
```

Повторный вызов с тем же ключом обновляет шифротекст и заменяет набор кампаний тех кабинетов, которые пришли в ответе.

Тот же `POST` для каждой кампании запрашивает отчёт по товарам и печатает тело ответа Маркета в лог API. В базу отчёт не пишется. Смотреть так:

```powershell
docker logs --tail 50 profitsamurai-api-1
```

Клиент — это компания. Сначала создаётся его база, затем к ней привязывается магазин. Старый `POST /cabinets` при этом остаётся.

```powershell
curl.exe -s -X POST http://localhost:8000/clients -H "Content-Type: application/json" -H "X-Bootstrap-Token: change-me" -d "{\`"name\`":\`"Магазин\`"}"
```

В ответе будут `id`, `database_name` и `status`. Если статус `failed`, повтор с тем же id:

```powershell
curl.exe -s -X POST http://localhost:8000/clients/ID/provision -H "X-Bootstrap-Token: change-me"
```

Когда статус `ready`, тот же Api-Key записывает кампании и модель размещения в базу клиента:

```powershell
curl.exe -s -X POST http://localhost:8000/clients/ID/shops -H "Content-Type: application/json" -H "X-Bootstrap-Token: change-me" -d "{\`"api_key\`":\`"КЛЮЧ\`"}"
```

Прочитать уже записанный кабинет:

```powershell
curl.exe -s http://localhost:8000/cabinets/1 -H "X-Bootstrap-Token: change-me"
```
