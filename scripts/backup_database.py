"""Online SQLite backup and isolated restore verification; never overwrites a database."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError('源数据库不存在')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create prevents accidental overwriting, including the live database.
    with destination.open('xb'):
        pass
    destination.chmod(0o600)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    return verify_restore(destination)


def verify_restore(backup):
    """Restore into a fresh isolated directory and compare every table count."""
    with tempfile.TemporaryDirectory(prefix='icu-restore-') as directory:
        restored = Path(directory) / 'restored.sqlite3'
        with sqlite3.connect(Path(backup).resolve().as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(restored) as dst:
                src.backup(dst)
                result = dst.execute('PRAGMA integrity_check').fetchone()[0]
                if result != 'ok':
                    raise RuntimeError('恢复完整性检查失败')
                tables = [row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                counts = {}
                for table in tables:
                    quoted = '"' + table.replace('"', '""') + '"'
                    original = src.execute('SELECT COUNT(*) FROM ' + quoted).fetchone()[0]
                    actual = dst.execute('SELECT COUNT(*) FROM ' + quoted).fetchone()[0]
                    if original != actual:
                        raise RuntimeError('恢复行数不一致: ' + table)
                    counts[table] = actual
                return {'integrity': result, 'restored_in_isolation': True, 'table_count': len(counts), 'rows': counts}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(Path(__file__).resolve().parents[1] / 'db.sqlite3'))
    parser.add_argument('--output')
    args = parser.parse_args()
    destination = args.output or str(Path(__file__).resolve().parents[1] / 'backups' / ('icu-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3'))
    report = backup_database(args.source, destination)
    print(json.dumps({'backup': destination, **report}, ensure_ascii=False, indent=2))
