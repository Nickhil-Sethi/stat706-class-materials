import argparse
from collections import OrderedDict
from contextlib import contextmanager
import datetime as dt
from dateutil import parser as date_parser
from enum import Enum
import io
import json
import logging
import os
import pandas as pd
import psycopg2
from psycopg2 import pool
from psycopg2.extras import register_json, Json
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT, register_adapter
import sys
from typing import List, Tuple, Dict

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)

register_adapter(dict, Json)


class PostgresType(Enum):
    TEXT = "TEXT"
    JSONB = "JSONB"
    INT = "INTEGER"
    DEC = "DECIMAL(15, 2)"
    SMALL_DEC = "DECIMAL(2,1)"
    BIGINT = "BIGINT"
    TIMESTAMP = "TIMESTAMP"
    DATE = "DATE"


def to_date(x):
    try:
        return date_parser.parse(str(x))
    except Exception as e:
        logging.warning(f"to_date {e}")
    finally:
        return None


def strip_prefix(x: str):
    try:
        return x.replace("tt0", "")
    except Exception as e:
        logging.warning(str(e))
    finally:
        return None


def to_float(x):
    try:
        return float(x)
    except Exception as e:
        logging.warning(f"to_float: {e}")
    finally:
        return None


def safe_int(x):
    try:
        return int(x)
    except Exception as e:
        logging.warning(f"to_int: {e}")
    finally:
        return None


def correct_json(bad_json: str):
    try:
        good_json = bad_json.replace("'", '"')
        obj = json.loads(good_json)
        return obj
    except Exception as e:
        logging.warning(f"correct_json {e}")
        return None


SCHEMAS: Dict[str, Dict[str, Tuple]] = {
    "movies_metadata": {
        # TODO(nickhil): could just make this text
        # this is giving me trouble
        "genres": (PostgresType.TEXT, correct_json),
        # TODO(nickhil): why does strip prefix throw an
        # error here
        "imdb_id": (PostgresType.TEXT, None),
        # TODO(nickhil): these aren't showing up
        "revenue": (PostgresType.TEXT, None),
        "budget": (PostgresType.TEXT, None),
        "original_title": (PostgresType.TEXT, None),
        # TODO(nickhil): this column is causing problems
        # "overview": PostgresType.TEXT,
        "release_date": (PostgresType.DATE, to_date),
    },
    "ratings": {
        "rating": (PostgresType.SMALL_DEC, None),
        "userId": (PostgresType.BIGINT, None),
        "movieId": (PostgresType.BIGINT, None),
        "timestamp": (PostgresType.TIMESTAMP, dt.datetime.utcfromtimestamp),
    },
    "links": {
        "movieId": (PostgresType.TEXT, None),
        "imdbId": (PostgresType.TEXT, None),
        "tmdbId": (PostgresType.TEXT, None),
    },
}


@contextmanager
def get_connection(isolation_level: str = None):
    global CONNECTION_POOL
    con = CONNECTION_POOL.getconn()
    if isolation_level:
        con.set_isolation_level(isolation_level)
    try:
        yield con
    finally:
        con.reset()
        CONNECTION_POOL.putconn(con)


def get_csv_length(csvname):
    with open(csvname, "r") as csv:
        length = sum(1 for row in csv)
    return length


def create_database(conn: "psycopg2.connection", dbname: str):
    conn.cursor().execute(f"CREATE DATABASE {dbname}")


def create_table_sql(tablename: str, schema: Dict[str, Tuple]):
    base_str = f"DROP TABLE IF EXISTS {tablename};\n"
    base_str += f"CREATE TABLE IF NOT EXISTS {tablename}(\n"
    index = 0
    for colname, (coltype, _) in schema.items():
        base_str += f"{colname} {coltype.value}"
        if index < len(schema) - 1:
            base_str += ",\n"
        index += 1
    base_str += ");"
    return base_str


def create_table(conn: "psycopg2.connection", csv_file: str, tablename: str):
    schema = SCHEMAS[tablename]
    create_sql = create_table_sql(tablename, schema)
    conn.cursor().execute(create_sql)


def load_csv(csv_file: str):
    tablename = csv_file.replace(".csv", "")

    logging.info(f"Creating table {tablename}")
    with get_connection() as conn:
        create_table(conn, csv_file, tablename)
        conn.commit()

    logging.info(f"Loading CSV {csv_file} into {tablename}")
    with get_connection() as conn:
        stream_csv_to_table(conn, csv_file, tablename)
        conn.commit()


def stream_csv_to_table(
    connection: "psycopg2.connection",
    csv_file: str,
    tablename: str,
    chunksize: int = 5000,
):
    db_cursor = connection.cursor()

    # just for metrics reporting
    rows_written = 0
    total_rows = get_csv_length(csv_file)
    columns = SCHEMAS[tablename].keys()
    schema = SCHEMAS[tablename]
    df_chunked = pd.read_csv(csv_file, chunksize=chunksize, usecols=columns)

    for df in df_chunked:
        for col in columns:
            transformation = schema[col][1]
            if transformation is None:
                continue
            df[col] = df[col].apply(schema[col][1])

        buffer = io.StringIO()
        df.to_csv(buffer, header=False, index=False, sep="|", na_rep="NULL")
        buffer.seek(0)
        try:
            db_cursor.copy_from(
                buffer, tablename, columns=list(df.columns), sep="|", null="NULL"
            )
        except Exception as e:
            logging.error(str(e))
            connection.rollback()
            raise e

        rows_written += len(df)
        pct_written = 100 * (rows_written / total_rows)

        logging.info(
            f"{tablename} {rows_written} / {total_rows} ({pct_written:.2f}%) written"
        )

    # commit the transaction
    # and close
    connection.commit()
    return


def create_data_table():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM movie_genres;")
        genres = cursor.fetchall()
        logging.info(f"Found genres {genres}")

    genre_names = [g[1] for g in genres]
    genre_cols = (",\n").join(
        [
            f"\tgenre_{name.replace(' ', '_').lower()} BOOLEAN DEFAULT FALSE"
            for name in genre_names
        ]
    )
    create_sql = f"""
    DROP TABLE project_data; 
    CREATE TABLE project_data (
        imdb_id INTEGER,
        movie_id INTEGER,
        average_rating DEC(2,1),
        revenue DEC(12, 1),
        budget DEC(32, 1),
        original_title TEXT,
        release_date DATE,
        {genre_cols}
    );"""

    with get_connection() as conn:
        cursor = conn.cursor()
        logging.info(f"Running: {create_sql}")
        cursor.execute(create_sql)
        conn.commit()
    return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--db_user", required=False, default=os.environ.get("DB_USER"), type=str
    )
    parser.add_argument(
        "--db_host", required=False, default=os.environ.get("DB_HOST"), type=str
    )
    parser.add_argument(
        "--db_pass", required=False, default=os.environ.get("DB_PASS"), type=str
    )
    parser.add_argument(
        "--db_port", required=False, default=os.environ.get("DB_PORT"), type=int
    )

    parser.add_argument("--load_data", required=False, default=False, type=bool)

    parser.add_argument(
        "--create-project-table", required=False, default=False, type=bool
    )
    args = parser.parse_args()

    MIN_CONNECTIONS = 1
    MAX_CONNECTIONS = 3

    CONNECTION_POOL = pool.SimpleConnectionPool(
        MIN_CONNECTIONS,
        MAX_CONNECTIONS,
        host=args.db_host,
        user=args.db_user,
        password=args.db_pass,
        port=args.db_port,
    )

    CSV_FILES = [
        # genres, budget, revenue, imdbid
        "movies_metadata.csv",
        # userid, movieid, ratings
        "ratings.csv",
        # *
        "links.csv",
    ]
    if args.load_data:
        for csv_file in CSV_FILES:
            load_csv(csv_file)
    else:
        create_data_table()
