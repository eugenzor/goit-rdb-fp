import re
from pathlib import Path

import duckdb
import gdown
import pandas as pd
import pgserver
import sqlparse
from sqlalchemy import create_engine, make_url, text

BASE_DIR = Path(__file__).parent
PG_DATA_DIR = BASE_DIR / ".pg_data"
DATA_DIR = BASE_DIR / "data"

engine = None


def display(df: pd.DataFrame):
    print(df.to_string())


def header(title: str):
    line = "=" * 72
    print(f"\n{line}\n{title}\n{line}")


def run_sql(sql: str):
    with engine.begin() as conn:
        for statement in sqlparse.split(sql):
            statement = statement.strip()
            if not statement:
                continue
            result = conn.execute(text(statement))
            if result.returns_rows:
                display(pd.DataFrame(result.fetchall(), columns=result.keys()))
            else:
                print(f"OK: {result.rowcount} rows")


def q(identifier: str) -> str:
    """Безпечне SQL-квотування імені колонки з перевіреного CSV."""
    return '"' + identifier.replace('"', '""') + '"'


def show_version():
    header("PostgreSQL")
    run_sql("SELECT version() AS postgresql_version;")


def create_schema():
    # 1. Створення схеми, завантаження CSV і перегляд даних
    # 1.1. Створення pandemic і встановлення search_path
    header("1.1. Створення схеми pandemic")
    run_sql("""
        CREATE SCHEMA IF NOT EXISTS pandemic;
        SET search_path TO pandemic, public;
        SELECT current_schema() AS active_schema;
    """)


def download_csv():
    # 1.2. Завантаження офіційного CSV за посиланням
    global csv_path
    header("1.2. Завантаження CSV")
    DATA_DIR.mkdir(exist_ok=True)
    csv_path = DATA_DIR / "infectious_cases.csv"
    file_id = "1dQf-uVXzYPYYGgXsG1y4Q1qDdhP2miZl"

    try:
        downloaded = gdown.download(
            id=file_id,
            output=str(csv_path),
            quiet=False,
        )
    except Exception as exc:
        downloaded = None
        print(f"Автоматичне завантаження недоступне: {exc}")

    if not downloaded or not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise FileNotFoundError(
            f"Не вдалося отримати infectious_cases.csv; "
            f"завантажте його вручну з посилання в умові до {csv_path}"
        )

    print(f"CSV: {csv_path} | {csv_path.stat().st_size:,} bytes")


def preview_csv():
    # 1.3. Огляд через DuckDB та перевірка в pandas
    global raw_df, columns, metric_columns
    header("1.3. Огляд через DuckDB та перевірка в pandas")

    # Я раніше ніколи не користувався DuckDB тому було цікаво поексперементувати з ним
    con = duckdb.connect(database=":memory:")

    csv_summary = con.execute("""
        SELECT COUNT(*) AS source_rows
        FROM read_csv(?, header=true, all_varchar=true)
    """, [str(csv_path)]).df()

    display(csv_summary)
    display(con.execute("""
        SELECT *
        FROM read_csv(?, header=true, all_varchar=true)
        LIMIT 5
    """, [str(csv_path)]).df())

    # Для імпорту використовуємо текстовий staging: жодних прихованих приведень типів.
    raw_df = pd.read_csv(
        csv_path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
        low_memory=False,
    )
    raw_df.columns = [str(column).strip().lstrip("﻿") for column in raw_df.columns]

    # Оригінальні заголовки стають SQL-ідентифікаторами — перевіряємо їх.
    assert raw_df.columns.is_unique, "CSV містить дублікати назв колонок"
    assert all(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", c) for c in raw_df.columns), (
        "Неочікувані назви колонок у CSV; перевірте заголовки файлу"
    )

    # Приводимо лише обов'язкові заголовки до назв, заданих у завданні.
    required_columns = ["Entity", "Code", "Year", "Number_rabies"]
    column_lookup = {col.lower(): col for col in raw_df.columns}
    missing = [col for col in required_columns if col.lower() not in column_lookup]
    if missing:
        raise ValueError(f"У CSV бракує обов'язкових колонок: {missing}")
    raw_df = raw_df.rename(columns={column_lookup[c.lower()]: c for c in required_columns})

    assert raw_df["Entity"].str.strip().ne("").all(), "Порожні значення Entity"
    assert raw_df["Year"].str.strip().str.fullmatch(r"[0-9]{1,4}").all(), (
        "Некоректні значення Year (очікуються цілі роки)"
    )

    columns = list(raw_df.columns)
    metric_columns = [col for col in columns if col not in {"Entity", "Code", "Year"}]
    assert "Number_rabies" in metric_columns

    print(f"Рядків: {len(raw_df):,} | колонок: {len(columns)}")
    print("Колонки:", columns)
    display(raw_df.head())


def import_raw():
    # 1.4. Імпорт сирих даних
    header("1.4. Імпорт сирих даних")
    staging_column_definitions = ",\n    ".join(f"{q(col)} TEXT" for col in columns)

    run_sql(f"""
        DROP TABLE IF EXISTS pandemic.disease_statistics;
        DROP TABLE IF EXISTS pandemic.entities;
        DROP TABLE IF EXISTS pandemic.infectious_cases;

        CREATE TABLE pandemic.infectious_cases (
            {staging_column_definitions}
        );
    """)

    raw_df.to_sql(
        name="infectious_cases",
        con=engine,
        schema="pandemic",
        if_exists="append",
        index=False,
        chunksize=1000,
        method="multi",
    )

    run_sql("""
        SELECT COUNT(*) AS imported_rows
        FROM pandemic.infectious_cases;

        SELECT *
        FROM pandemic.infectious_cases
        LIMIT 5;
    """)


def create_normalized_tables():
    # 2. Нормалізація до 3НФ
    header("2. Нормалізація до 3НФ")
    metric_definitions = ",\n    ".join(
        f"{q(col)} NUMERIC" for col in metric_columns
    )

    run_sql(f"""
        CREATE TABLE pandemic.entities (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            "Entity" TEXT NOT NULL CHECK ("Entity" <> ''),
            "Code" TEXT,
            CONSTRAINT uq_entities_entity_code
                UNIQUE NULLS NOT DISTINCT ("Entity", "Code")
        );

        CREATE TABLE pandemic.disease_statistics (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            entity_id BIGINT NOT NULL REFERENCES pandemic.entities(id),
            "Year" INTEGER NOT NULL CHECK ("Year" BETWEEN 1 AND 9999),
            {metric_definitions}
        );

        CREATE INDEX ix_disease_statistics_entity_year
            ON pandemic.disease_statistics (entity_id, "Year");
    """)


def fill_entities():
    # 2.2. Заповнення довідника унікальних комбінацій Entity, Code
    header("2.2. Заповнення довідника Entity, Code")
    run_sql("""
        INSERT INTO pandemic.entities ("Entity", "Code")
        SELECT DISTINCT
            BTRIM("Entity") AS "Entity",
            NULLIF(BTRIM("Code"), '') AS "Code"
        FROM pandemic.infectious_cases
        WHERE NULLIF(BTRIM("Entity"), '') IS NOT NULL
        ON CONFLICT ON CONSTRAINT uq_entities_entity_code DO NOTHING;

        SELECT COUNT(*) AS unique_entity_code_pairs
        FROM pandemic.entities;

        SELECT *
        FROM pandemic.entities
        ORDER BY id
        LIMIT 10;
    """)


def fill_disease_statistics():
    # 2.3. Перенесення всіх кількісних показників
    header("2.3. Перенесення кількісних показників")
    # Простий числовий формат
    number_pattern = r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?$"

    numeric_selects = []
    for column in metric_columns:
        normalized_value = f"REPLACE(BTRIM(src.{q(column)}), ',', '')"
        numeric_selects.append(f"""
            CASE
                WHEN {normalized_value} ~ '{number_pattern}'
                THEN {normalized_value}::NUMERIC
                ELSE NULL
            END AS {q(column)}
        """)

    metrics_sql = ",\n".join(numeric_selects)
    insert_columns = ", ".join(q(col) for col in metric_columns)

    run_sql(f"""
        INSERT INTO pandemic.disease_statistics (
            entity_id, "Year", {insert_columns}
        )
        SELECT
            dim.id AS entity_id,
            BTRIM(src."Year")::INTEGER AS "Year",
            {metrics_sql}
        FROM pandemic.infectious_cases AS src
        JOIN pandemic.entities AS dim
            ON dim."Entity" = BTRIM(src."Entity")
           AND dim."Code" IS NOT DISTINCT FROM NULLIF(BTRIM(src."Code"), '');

        ANALYZE pandemic.entities;
        ANALYZE pandemic.disease_statistics;
    """)


def validate_normalization():
    # 2.4. Перевірка нормалізації
    header("2.4. Перевірка нормалізації")
    validation_query = """
        SELECT
            (SELECT COUNT(*) FROM pandemic.infectious_cases) AS original_rows,
            (SELECT COUNT(*) FROM pandemic.entities) AS entities_count,
            (SELECT COUNT(*) FROM pandemic.disease_statistics) AS facts_count,
            (
                SELECT COUNT(*)
                FROM pandemic.disease_statistics AS fact
                LEFT JOIN pandemic.entities AS dim ON dim.id = fact.entity_id
                WHERE dim.id IS NULL
            ) AS orphan_facts;
    """

    run_sql(validation_query)

    with engine.connect() as conn:
        counts = conn.execute(text(validation_query)).mappings().one()

    assert counts["original_rows"] == counts["facts_count"], (
        "Кількість рядків змінилася під час нормалізації"
    )
    assert counts["orphan_facts"] == 0, "Виявлено факти без відповідного Entity"
    print("✅ Кількість рядків збережена; зовнішні ключі коректні.")

    run_sql("""
        SELECT
            dim."Entity",
            dim."Code",
            fact."Year",
            fact."Number_rabies"
        FROM pandemic.disease_statistics AS fact
        JOIN pandemic.entities AS dim ON dim.id = fact.entity_id
        ORDER BY fact.id
        LIMIT 10;
    """)


def aggregate_rabies():
    # 3. Агрегація Number_rabies для кожної пари
    header("3. Агрегація Number_rabies")
    run_sql("""
        SELECT
            dim.id AS entity_id,
            dim."Entity",
            dim."Code",
            ROUND(AVG(fact."Number_rabies"), 2) AS avg_rabies,
            MIN(fact."Number_rabies") AS min_rabies,
            MAX(fact."Number_rabies") AS max_rabies,
            SUM(fact."Number_rabies") AS sum_rabies
        FROM pandemic.entities AS dim
        JOIN pandemic.disease_statistics AS fact
            ON fact.entity_id = dim.id
        GROUP BY dim.id, dim."Entity", dim."Code"
        ORDER BY avg_rabies DESC NULLS LAST, dim."Entity"
        LIMIT 10;
    """)


def year_difference():
    # 4. Колонки дати та різниці в роках за вбудованими SQL-функціями
    header("4. Колонки дати та різниці в роках")
    run_sql("""
        SELECT
            fact.id,
            dim."Entity",
            fact."Year",
            MAKE_DATE(fact."Year", 1, 1) AS first_january,
            CURRENT_DATE AS today,
            EXTRACT(
                YEAR FROM AGE(CURRENT_DATE, MAKE_DATE(fact."Year", 1, 1))
            )::INTEGER AS years_difference
        FROM pandemic.disease_statistics AS fact
        JOIN pandemic.entities AS dim ON dim.id = fact.entity_id
        ORDER BY fact.id
        LIMIT 10;
    """)


def years_since_function():
    # 5. Дві власні SQL-функції
    # 5.1. Функція для обчислення різниці в роках
    header("5.1. Функція years_since()")
    run_sql("""
        CREATE OR REPLACE FUNCTION pandemic.years_since(p_year INTEGER)
        RETURNS INTEGER
        LANGUAGE SQL
        STABLE
        AS $$
            SELECT EXTRACT(
                YEAR FROM AGE(CURRENT_DATE, MAKE_DATE(p_year, 1, 1))
            )::INTEGER;
        $$;

        SELECT
            1996 AS input_year,
            MAKE_DATE(1996, 1, 1) AS first_january,
            CURRENT_DATE AS today,
            pandemic.years_since(1996) AS years_difference;
    """)

    run_sql("""
        SELECT
            fact."Year",
            MAKE_DATE(fact."Year", 1, 1) AS first_january,
            CURRENT_DATE AS today,
            pandemic.years_since(fact."Year") AS years_difference
        FROM pandemic.disease_statistics AS fact
        ORDER BY fact.id
        LIMIT 10;
    """)


def cases_per_period_function():
    # 5.2. Функція для обчислення середньої кількості захворювань за період
    header("5.2. Функція cases_per_period()")
    run_sql("""
        CREATE OR REPLACE FUNCTION pandemic.cases_per_period(
            p_cases NUMERIC,
            p_divisor NUMERIC
        )
        RETURNS NUMERIC
        LANGUAGE SQL
        IMMUTABLE
        AS $$
            SELECT p_cases / NULLIF(p_divisor, 0);
        $$;

        SELECT
            pandemic.cases_per_period(120, 12) AS monthly_demo,
            pandemic.cases_per_period(120, 4) AS quarterly_demo,
            pandemic.cases_per_period(120, 2) AS half_year_demo,
            pandemic.cases_per_period(120, 0) AS zero_divisor_demo;
    """)

    run_sql("""
        SELECT
            dim."Entity",
            fact."Year",
            fact."Number_rabies" AS rabies_per_year,
            ROUND(
                pandemic.cases_per_period(fact."Number_rabies", 12), 2
            ) AS rabies_per_month,
            ROUND(
                pandemic.cases_per_period(fact."Number_rabies", 4), 2
            ) AS rabies_per_quarter,
            ROUND(
                pandemic.cases_per_period(fact."Number_rabies", 2), 2
            ) AS rabies_per_half_year
        FROM pandemic.disease_statistics AS fact
        JOIN pandemic.entities AS dim ON dim.id = fact.entity_id
        WHERE fact."Number_rabies" IS NOT NULL
        ORDER BY dim."Entity", fact."Year"
        LIMIT 20;
    """)


def check_results():
    # 6. Перевірка результатів
    header("6. Перевірка результатів")
    run_sql("""
        SELECT
            COUNT(*) AS fact_rows,
            COUNT("Number_rabies") AS numeric_rabies_rows,
            COUNT(*) - COUNT("Number_rabies") AS null_or_non_numeric_rabies_rows,
            MIN("Year") AS min_year,
            MAX("Year") AS max_year
        FROM pandemic.disease_statistics;

        SELECT
            BOOL_AND(
                pandemic.years_since("Year") =
                EXTRACT(
                    YEAR FROM AGE(CURRENT_DATE, MAKE_DATE("Year", 1, 1))
                )::INTEGER
            ) AS year_function_matches_builtin
        FROM pandemic.disease_statistics;

        SELECT
            pandemic.cases_per_period(120, 12) = 10 AS monthly_ok,
            pandemic.cases_per_period(120, 4) = 30 AS quarterly_ok,
            pandemic.cases_per_period(120, 2) = 60 AS half_year_ok,
            pandemic.cases_per_period(120, 0) IS NULL AS zero_division_safe;
    """)


def main():
    global engine
    pg = pgserver.get_server(PG_DATA_DIR)
    # SQLAlchemy 2.1+ defaults to psycopg 3, the project uses psycopg2
    url = make_url(pg.get_uri()).set(drivername="postgresql+psycopg2")
    engine = create_engine(url)
    try:
        show_version()
        create_schema()
        download_csv()
        preview_csv()
        import_raw()
        create_normalized_tables()
        fill_entities()
        fill_disease_statistics()
        validate_normalization()
        aggregate_rabies()
        year_difference()
        years_since_function()
        cases_per_period_function()
        check_results()
    finally:
        engine.dispose()
        pg.cleanup()


if __name__ == "__main__":
    main()
