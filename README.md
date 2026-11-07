# goit-rdb-fp

Фінальний проект

Студент: Євген Галенко

Група: AI&ML 4

## Встановлення

Потрібен [uv](https://docs.astral.sh/uv/).

Ініціалізація віртуального середовища
```bash
uv sync
source .venv/bin/activate
```

## Запуск

```bash
python main.py
```

Під час першого запуску датасет `infectious_cases.csv` завантажується з Google Drive у каталог `data/`, а PostgreSQL запускається локально через `pgserver` у каталозі `.pg_data/`.


# Reflection

1. Створено схему pandemic і завантажено повний набір infectious_cases.csv.
2. Дані нормалізовано до 3НФ у дві таблиці: entities (унікальні Entity / Code) і disease_statistics (рік, усі кількісні показники, зовнішній ключ).
3. Для Number_rabies виконано групування та обчислено середнє, мінімум, максимум і суму для кожної сутності; показано 10 записів із найбільшим середнім.
4. Дату 1 січня і різницю в повних роках отримано через MAKE_DATE, CURRENT_DATE, AGE, EXTRACT.
5. Створено та перевірено дві PostgreSQL-функції: years_since() і cases_per_period() (періоди 12 / 4 / 2).
