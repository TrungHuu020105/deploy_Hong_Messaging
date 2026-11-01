"""MySQL connection, transactions and portable persistence helpers.

Application connections use MySQL. Explicit SQLite paths are retained for
offline tests and the one-time migration reader; there is no automatic fallback.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MYSQL_KEYS = ("MYSQL_HOST","MYSQL_PORT","MYSQL_USER","MYSQL_PASSWORD","MYSQL_DATABASE")
APP_KEYS = ("APP_MODE","AUTO_SYNC_ENABLED","ALLOWED_HOSTS","WEB_EDIT_TOKEN","WEB_ALLOW_PUBLIC_EDITS")


def load_environment(path=ROOT / ".env"):
    values = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key,value = line.split("=",1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    for key in (*MYSQL_KEYS,*APP_KEYS,"PANCAKE_SHOP_ID","PANCAKE_API_KEY"):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def mysql_settings(values=None):
    values = load_environment() if values is None else values
    for key in ("MYSQL_HOST","MYSQL_USER","MYSQL_DATABASE"):
        if not str(values.get(key,"")).strip():
            raise ValueError(f"Thiếu {key}. Điền cấu hình MySQL trong .env hoặc biến môi trường.")
    try:
        port = int(values.get("MYSQL_PORT") or 3306)
    except (TypeError,ValueError):
        raise ValueError("MYSQL_PORT phải là số từ 1 đến 65535.") from None
    if not 1 <= port <= 65535:
        raise ValueError("MYSQL_PORT phải là số từ 1 đến 65535.")
    return {"host":str(values["MYSQL_HOST"]).strip(),"port":port,
            "user":str(values["MYSQL_USER"]).strip(),"password":values.get("MYSQL_PASSWORD", ""),
            "database":str(values["MYSQL_DATABASE"]).strip()}


def identifier(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*",name):
        raise ValueError("Tên bảng hoặc cột không hợp lệ.")
    return f"`{name}`"


def mysql_query(sql, params):
    """Convert bound placeholders, leaving question marks in SQL literals alone."""
    if params is None or (isinstance(params,(tuple,list,dict)) and not params):
        return sql, None
    named = isinstance(params,dict)
    output,quote,i = [],None,0
    while i < len(sql):
        ch = sql[i]
        if ch == "%":
            output.append("%%")
        elif quote:
            output.append(ch)
            if ch == "\\" and i+1 < len(sql):
                i += 1
                output.append(sql[i])
            elif ch == quote:
                if i+1 < len(sql) and sql[i+1] == quote:
                    i += 1
                    output.append(sql[i])
                else:
                    quote = None
        elif ch in "'\"`":
            quote = ch
            output.append(ch)
        elif ch == "?" and not named:
            output.append("%s")
        elif ch == ":" and named:
            match = re.match(r"[A-Za-z_][A-Za-z_0-9]*",sql[i+1:])
            if match:
                output.append(f"%({match[0]})s")
                i += len(match[0])
            else:
                output.append(ch)
        else:
            output.append(ch)
        i += 1
    return "".join(output),params


class Row:
    """sqlite.Row-compatible access by name/index, dict(row), and tuple(row)."""
    def __init__(self,keys,values):
        self._keys,self._values = tuple(keys),tuple(values)
        self._positions = {key:i for i,key in enumerate(keys)}
    def keys(self): return self._keys
    def __getitem__(self,key):
        return self._values[self._positions[key]] if isinstance(key,str) else self._values[key]
    def __iter__(self): return iter(self._values)
    def __len__(self): return len(self._values)


class Result:
    def __init__(self,cursor):
        self.lastrowid,self.rowcount = cursor.lastrowid,cursor.rowcount
        keys = [col[0] for col in cursor.description] if cursor.description else []
        self._rows = [Row(keys,row) for row in cursor.fetchall()] if keys else []
        self._position = 0
    def fetchone(self):
        if self._position >= len(self._rows): return None
        row = self._rows[self._position]
        self._position += 1
        return row
    def fetchall(self):
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows
    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class MySQLConnection:
    dialect = "mysql"
    def __init__(self,settings=None,*,initialize=True):
        import pymysql
        from pymysql.constants import CLIENT

        self.settings = mysql_settings(settings)
        self.in_transaction = False
        self._write_lock = False
        self._closed = False
        try:
            self.raw = pymysql.connect(**self.settings,charset="utf8mb4",collation="utf8mb4_bin",
                autocommit=True,connect_timeout=10,read_timeout=60,write_timeout=60,
                client_flag=CLIENT.FOUND_ROWS,
                init_command="SET SESSION sql_mode='STRICT_TRANS_TABLES,ONLY_FULL_GROUP_BY,NO_ENGINE_SUBSTITUTION'")
        except pymysql.MySQLError as error:
            code = error.args[0] if error.args else "unknown"
            raise ConnectionError(f"Không kết nối được MySQL (mã {code}). Kiểm tra 5 biến MYSQL_* và database đã được tạo.") from None
        server_info = str(self.raw.get_server_info())
        version = re.match(r"(\d+)\.(\d+)\.(\d+)",server_info)
        self.row_alias_upsert = ("mariadb" not in server_info.lower() and version is not None
                                 and tuple(map(int,version.groups())) >= (8,0,19))
        if initialize:
            try:
                self.initialize_schema()
            except BaseException:
                self.close()
                raise

    def lock_name(self,purpose):
        digest = hashlib.sha256(self.settings["database"].encode()).hexdigest()[:40]
        return f"hong:{purpose}:{digest}"

    def acquire_lock(self,name,timeout=0):
        with self.raw.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s,%s)",(name,timeout))
            return cursor.fetchone()[0] == 1

    def release_lock(self,name):
        with self.raw.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)",(name,))

    def initialize_schema(self):
        name = self.lock_name("schema")
        if not self.acquire_lock(name,30):
            raise RuntimeError("Không lấy được khóa khởi tạo schema MySQL.")
        try:
            script = (ROOT/"database"/"mysql_schema.sql").read_text(encoding="utf-8")
            for statement in script.split(";"):
                if statement.strip():
                    with self.raw.cursor() as cursor:
                        cursor.execute(statement)
        finally:
            self.release_lock(name)

    def begin_write(self):
        if self.in_transaction:
            raise RuntimeError("Đã có giao dịch đang mở.")
        if not self.acquire_lock(self.lock_name("write"),30):
            raise RuntimeError("MySQL đang bận ghi dữ liệu. Hãy thử lại.")
        self._write_lock = True
        try:
            self.raw.begin()
            self.in_transaction = True
        except BaseException:
            self.release_lock(self.lock_name("write"))
            self._write_lock = False
            raise

    def execute(self,sql,params=None):
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b",sql,re.I) and not self.in_transaction:
            self.begin_write()
        sql,params = mysql_query(sql,params)
        with self.raw.cursor() as cursor:
            cursor.execute(sql,params)
            return Result(cursor)

    def executemany(self,sql,rows):
        rows = list(rows)
        if not rows:
            return self.execute("SELECT 1 WHERE 1=0")
        if not self.in_transaction:
            self.begin_write()
        sql,_ = mysql_query(sql,rows[0])
        with self.raw.cursor() as cursor:
            cursor.executemany(sql,rows)
            return Result(cursor)

    def _end(self,commit):
        if not self.in_transaction:
            return
        try:
            self.raw.commit() if commit else self.raw.rollback()
        except BaseException:
            self.close()
            raise
        finally:
            self.in_transaction = False
        if self._write_lock:
            self._write_lock = False
            self.release_lock(self.lock_name("write"))

    def commit(self): self._end(True)
    def rollback(self): self._end(False)
    def __enter__(self): return self
    def __exit__(self,exc_type,*_):
        self.rollback() if exc_type else self.commit()
        return False
    def close(self):
        if not self._closed:
            self._closed = True
            self.raw.close()  # Disconnect rolls back and releases all advisory locks.
            self.in_transaction = False
            self._write_lock = False


def is_mysql(db): return getattr(db,"dialect",None) == "mysql"


def begin_write(db):
    db.begin_write() if is_mysql(db) else db.execute("BEGIN IMMEDIATE")


def upsert(db,table,values,keys,updates):
    sql,params = upsert_query(db,table,values,keys,updates)
    return db.execute(sql,params)


def upsert_query(db,table,values,keys,updates):
    cols = list(values)
    sql = f"INSERT INTO {identifier(table)} ({','.join(map(identifier,cols))}) VALUES ({','.join('?' for _ in cols)})"
    params = list(values.values())
    if is_mysql(db):
        sql += " ON DUPLICATE KEY UPDATE "+",".join(f"{identifier(col)}=?" for col in updates)
        params += [values[col] for col in updates]
    else:
        sql += f" ON CONFLICT({','.join(map(identifier,keys))}) DO UPDATE SET "+",".join(
            f"{identifier(col)}=excluded.{identifier(col)}" for col in updates)
    return sql,params


def upsert_many(db,table,rows,keys,updates):
    rows = list(rows)
    if not rows:
        return
    columns = list(rows[0])
    if is_mysql(db):
        values = ",".join("("+",".join("?" for _ in columns)+")" for _ in rows)
        sql = f"INSERT INTO {identifier(table)} ({','.join(map(identifier,columns))}) VALUES {values}"
        if getattr(db,"row_alias_upsert",True):
            sql += " AS incoming ON DUPLICATE KEY UPDATE "+",".join(f"{identifier(col)}=incoming.{identifier(col)}" for col in updates)
        else:
            sql += " ON DUPLICATE KEY UPDATE "+",".join(f"{identifier(col)}=VALUES({identifier(col)})" for col in updates)
        return db.execute(sql,[row[col] for row in rows for col in columns])
    sql,_ = upsert_query(db,table,rows[0],keys,updates)
    return db.executemany(sql,[[row[col] for col in columns] for row in rows])


def insert_if_absent(db,table,values,keys):
    cols = list(values)
    sql = f"INSERT INTO {identifier(table)} ({','.join(map(identifier,cols))}) VALUES ({','.join('?' for _ in cols)})"
    if is_mysql(db):
        # Unlike INSERT IGNORE this does not suppress invalid data or FK errors.
        key = identifier(keys[0])
        sql += f" ON DUPLICATE KEY UPDATE {key}={key}"
    else:
        sql += f" ON CONFLICT({','.join(map(identifier,keys))}) DO NOTHING"
    return db.execute(sql,list(values.values()))


def concat_sql(db,*parts):
    return f"CONCAT({','.join(parts)})" if is_mysql(db) else "||".join(parts)


def json_present_sql(db,column,path):
    expression = f"json_extract({column},'{path}')"
    return f"({expression} IS NOT NULL AND JSON_TYPE({expression}) != 'NULL')" if is_mysql(db) else f"{expression} IS NOT NULL"
