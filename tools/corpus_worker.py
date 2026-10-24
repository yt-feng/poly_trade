#!/usr/bin/env python3
"""Private, resumable corpus extraction. Run research processing in cloud only.

This program never executes corpus code or credits semantic learning. All
outputs are private. Console output contains aggregate operational counts only.
"""
from __future__ import annotations

import argparse
import codecs
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import tarfile
import time
import zipfile

VERSION = 'quantml-private-cloud-extractor-v1'
TEXT = {'.txt', '.md', '.py', '.json', '.jsonl', '.ipynb', '.r', '.js', '.ts',
        '.html', '.htm', '.xml', '.yaml', '.yml', '.toml', '.sql', '.tex'}
MEDIA = {'.mp4', '.m4v', '.mov', '.mkv', '.avi', '.mp3', '.wav', '.m4a'}
ARCHIVES = {'.zip', '.tar', '.tgz', '.gz', '.bz2', '.xz', '.7z', '.rar'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def safe_relative(value):
    """Reject traversal, Windows drive paths and ambiguous archive separators."""
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('invalid_relative_path')
    p = PurePosixPath(value)
    if not p.parts or p.is_absolute() or any(part in ('..', '') for part in p.parts) or ':' in p.parts[0]:
        raise ValueError('unsafe_relative_path')
    return p


def member_storage_name(index, name):
    """Index is essential: ZIP permits distinct members with identical names."""
    return f'{index:09d}-' + sha(name.encode()) + Path(name).suffix[:12]


def parser_protocol(extension, args):
    """Cursor units cannot be reinterpreted by a later parameter change."""
    protocol = {'version': VERSION, 'extension': extension}
    if extension in TEXT:
        protocol.update(encoding=args.text_encoding, chunk_chars=args.text_chunk_chars)
    elif extension in ('.csv', '.tsv', '.pq', '.parquet'):
        protocol.update(encoding=args.text_encoding if extension in ('.csv', '.tsv') else None,
                        batch_rows=args.table_rows)
    elif extension == '.pdf':
        protocol['unit'] = 'page'
        # Adding OCR revisits only explicit OCR gaps; per-page options are recorded.
    return protocol


def contained_file(root, relative):
    rel = safe_relative(relative)
    path = root.joinpath(*rel.parts)
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('symlink_source_forbidden')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('source_outside_root')
    return path


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)


class BudgetStop(Exception):
    pass


def cloud_guard(backend, preflight):
    required = ('input_scope_and_bytes', 'estimated_cpu_memory_runtime', 'cache_identity',
                'concurrency', 'timeout_seconds', 'cost_or_quota_bound', 'private_output_destination')
    if not all(preflight.get(k) is not None for k in required):
        raise ValueError('incomplete_cloud_preflight')
    if preflight.get('backend') != backend or preflight.get('backend_verified') is not True:
        raise ValueError('unverified_cloud_backend')
    if preflight.get('concurrency') != 1:
        raise ValueError('single_writer_required')
    if backend == 'github-actions':
        if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
            raise ValueError('github_hosted_runner_required')
    elif backend == 'aws':
        if os.environ.get('QUANTML_VERIFIED_CLOUD_BACKEND') != 'aws':
            raise ValueError('verified_aws_context_required')
    else:
        raise ValueError('unsupported_backend')


class Worker:
    def __init__(self, args, preflight):
        cloud_guard(args.backend, preflight)
        self.args = args
        self.root = Path(args.input_root).resolve()
        self.out = Path(args.output_root).resolve()
        if self.out != Path(preflight['private_output_destination']).resolve():
            raise ValueError('output_destination_preflight_mismatch')
        if args.max_seconds > float(preflight['timeout_seconds']):
            raise ValueError('timeout_exceeds_preflight')
        if self.root == self.out or self.out.is_relative_to(self.root):
            raise ValueError('outputs_must_not_be_inside_input_corpus')
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.started = time.monotonic()
        self.units_written = 0
        self.bytes_written = 0
        self.db = sqlite3.connect(self.out / 'state.sqlite3')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL,
            source_root TEXT NOT NULL, expected_bytes INTEGER,
            expected_hash TEXT, parent_id TEXT, archive_depth INTEGER DEFAULT 0,
            input_signature TEXT, content_hash TEXT, status TEXT NOT NULL,
            cursor INTEGER NOT NULL DEFAULT 0, total_units INTEGER,
            unit_kind TEXT, duplicate_of TEXT, notes TEXT NOT NULL DEFAULT '{}',
            updated_utc TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS units (
            source_id TEXT NOT NULL, unit_index INTEGER NOT NULL,
            unit_kind TEXT NOT NULL, artifact TEXT, content_hash TEXT,
            status TEXT NOT NULL, metadata TEXT NOT NULL,
            PRIMARY KEY(source_id, unit_index));
          CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, started_utc TEXT, finished_utc TEXT,
            config TEXT, result TEXT);
          CREATE TABLE IF NOT EXISTS source_protocols (
            source_id TEXT PRIMARY KEY, protocol TEXT NOT NULL);
        ''')
        self.db.row_factory = sqlite3.Row
        self.run_id = str(time.time_ns())
        self.db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',
                        (self.run_id, now(), None, canonical({'version': VERSION, 'preflight': preflight,
                            'max_files': args.max_files, 'max_units': args.max_units,
                            'max_seconds': args.max_seconds, 'max_output_bytes': args.max_output_bytes,
                            'max_memory_mib': args.max_memory_mib,
                            'max_page_pixels': args.max_page_pixels}).decode(), None))
        self.db.commit()

    def check(self, more_bytes=0):
        if time.monotonic() - self.started >= self.args.max_seconds:
            raise BudgetStop('time_budget')
        if self.units_written >= self.args.max_units:
            raise BudgetStop('unit_budget')
        if self.bytes_written + more_bytes > self.args.max_output_bytes:
            raise BudgetStop('output_budget_requires_larger_cloud_batch')

    def register(self, entry, root=None, parent=None, depth=0):
        rel = str(safe_relative(entry.get('relative_path', entry.get('path'))))
        sid = entry.get('source_id') or sha((str(parent) + ':' + rel).encode())
        if not isinstance(sid, str) or not sid or len(sid) > 128:
            raise ValueError('invalid_source_id')
        # File paths in output use a hash, not a caller-controlled source ID.
        expected = entry.get('size_bytes', entry.get('bytes'))
        expected_hash = entry.get('sha256') or entry.get('content_sha256')
        existing = self.db.execute('SELECT * FROM sources WHERE source_id=?', (sid,)).fetchone()
        target_root = str(root or self.root)
        if existing and (existing['relative_path'] != rel or existing['source_root'] != target_root or
                         existing['expected_bytes'] != expected or existing['expected_hash'] != expected_hash):
            raise ValueError('manifest_identity_changed')
        self.db.execute('INSERT OR IGNORE INTO sources (source_id,relative_path,source_root,expected_bytes,expected_hash,parent_id,archive_depth,status,updated_utc) VALUES (?,?,?,?,?,?,?,?,?)',
                        (sid, rel, target_root, expected, expected_hash, parent, depth, 'inventoried', now()))
        self.db.commit()
        return sid

    def state(self, sid, **fields):
        if 'notes' in fields:
            fields['notes'] = canonical(fields['notes']).decode()
        fields['updated_utc'] = now()
        self.db.execute('UPDATE sources SET ' + ','.join(k + '=?' for k in fields) + ' WHERE source_id=?', tuple(fields.values()) + (sid,))
        self.db.commit()

    def unit(self, sid, index, kind, data=None, metadata=None, status='extracted', suffix='json'):
        raw = data if isinstance(data, bytes) else canonical(data) if data is not None else None
        self.check(0 if raw is None else len(raw))
        artifact = None
        if raw is not None:
            p = self.out / 'units' / sha(sid.encode()) / (f'{index:09d}.{suffix}')
            atomic(p, raw)
            artifact = str(p.relative_to(self.out))
            self.bytes_written += len(raw)
        self.db.execute('INSERT OR REPLACE INTO units VALUES (?,?,?,?,?,?,?)',
                        (sid, index, kind, artifact, None if raw is None else sha(raw), status,
                         canonical(metadata or {}).decode()))
        self.db.commit()
        self.units_written += 1

    def bind_protocol(self, row, extension):
        value = canonical(parser_protocol(extension, self.args)).decode()
        existing = self.db.execute('SELECT protocol FROM source_protocols WHERE source_id=?', (row['source_id'],)).fetchone()
        if existing and existing['protocol'] != value:
            self.state(row['source_id'], status='pending_protocol_change',
                       notes={'reason': 'cursor_units_would_change', 'required': 'new versioned output root; preserve existing artifacts'})
            return False
        if not existing:
            if row['cursor']:
                self.state(row['source_id'], status='pending_legacy_protocol_binding',
                           notes={'required': 'verify previous parser parameters before binding; do not guess cursor units'})
                return False
            self.db.execute('INSERT INTO source_protocols VALUES (?,?)', (row['source_id'], value))
            self.db.commit()
        return True

    def verify(self, row):
        p = contained_file(Path(row['source_root']), row['relative_path'])
        if not p.is_file():
            self.state(row['source_id'], status='pending_transfer')
            return None
        st = p.stat()
        sig = f'{st.st_size}:{st.st_mtime_ns}:{st.st_ino}'
        if row['expected_bytes'] is not None and st.st_size != row['expected_bytes']:
            self.state(row['source_id'], status='blocked_size_mismatch')
            return None
        if row['content_hash'] and row['input_signature'] == sig:
            return p
        if row['content_hash']:
            self.state(row['source_id'], status='blocked_source_changed')
            return None
        digest = hashlib.sha256()
        with p.open('rb') as stream:
            while True:
                self.check()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = p.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            self.state(row['source_id'], status='blocked_source_changed_during_hash')
            return None
        value = digest.hexdigest()
        if row['expected_hash'] and row['expected_hash'] != value:
            self.state(row['source_id'], status='blocked_hash_mismatch')
            return None
        # A duplicate is linked; it is NOT independently marked extracted/read.
        candidates = self.db.execute('SELECT source_id,relative_path FROM sources WHERE content_hash=? AND source_id!=? AND duplicate_of IS NULL ORDER BY source_id',
                                     (value, row['source_id'])).fetchall()
        # Equal bytes with a different format need their own extraction contract
        # (e.g. .txt versus .csv); a byte duplicate cannot bypass dataset audit.
        same = next((candidate for candidate in candidates
                     if Path(candidate['relative_path']).suffix.lower() == p.suffix.lower()), None)
        self.state(row['source_id'], status='duplicate_linked' if same else 'transferred',
                   content_hash=value, input_signature=sig,
                   duplicate_of=same['source_id'] if same else None)
        return None if same else p

    def text(self, row, path):
        sid = row['source_id']
        with path.open('rb') as stream:
            prefix = stream.read(4)
        encoding = self.args.text_encoding or ('utf-16' if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else 'utf-8-sig')
        # On resume re-read previous chunks in cloud so character offsets remain exact.
        offset = 0
        count = 0
        with path.open('r', encoding=encoding, errors='strict', newline='') as stream:
            while True:
                self.check()
                self.check(self.args.text_chunk_chars * 4)
                text = stream.read(self.args.text_chunk_chars)
                if not text:
                    break
                if count >= row['cursor']:
                    self.unit(sid, count, 'characters', text.encode(),
                              {'start_character': offset, 'end_character': offset + len(text), 'encoding': encoding}, suffix='txt')
                    self.state(sid, status='extracting', cursor=count + 1, unit_kind='text_chunks')
                offset += len(text)
                count += 1
        self.state(sid, status='extracted', cursor=count, total_units=count, unit_kind='text_chunks',
                   notes={'characters': offset, 'encoding': encoding, 'code_executed': False,
                          'notebook_outputs_are_untrusted_stored_content': path.suffix.lower() == '.ipynb'})

    def pdf(self, row, path):
        try:
            import pymupdf
        except ImportError:
            self.state(row['source_id'], status='pending_pdf_dependency')
            return
        sid = row['source_id']
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                self.state(sid, status='blocked_encrypted_pdf')
                return
            self.state(sid, total_units=len(doc), unit_kind='pages')
            pending_indices = [r[0] for r in self.db.execute("SELECT unit_index FROM units WHERE source_id=? AND status IN ('pending_ocr','pending_visual_resolution')", (sid,))] if self.args.retry_pending else []
            indices = sorted(set(pending_indices) | set(range(row['cursor'], len(doc))))
            for number in indices:
                self.check()
                page = doc[number]
                text = page.get_text('text', sort=True)
                raster_images = len(page.get_images())
                drawings = len(page.get_drawings())
                area = max(1.0, float(page.rect.width) * float(page.rect.height))
                needs_ocr = len(text.strip()) < 40 or raster_images > 0
                ocr_status = 'not_required'
                if needs_ocr:
                    if area * (150 / 72) ** 2 > self.args.max_page_pixels:
                        ocr_status = 'pending_ocr_render_capacity'
                    elif self.args.pdf_ocr and shutil.which('tesseract'):
                        self.check(int(area * (150 / 72) ** 2 * 4) + 65536)
                        try:
                            tp = page.get_textpage_ocr(language=self.args.ocr_languages, dpi=150, full=True)
                            text = page.get_text('text', textpage=tp, sort=True)
                            ocr_status = 'performed_unverified'
                        except Exception as exc:
                            ocr_status = 'pending_ocr_' + type(exc).__name__
                    else:
                        ocr_status = 'pending_ocr_capability'
                visual_path = None
                render_scale = None
                if raster_images or drawings or needs_ocr:
                    render_scale = min(1.0, math.sqrt(self.args.max_page_pixels / area))
                    # Check the uncompressed allocation estimate before creating a pixmap.
                    self.check(int(area * render_scale * render_scale * 4) + 65536)
                    rendered = page.get_pixmap(matrix=pymupdf.Matrix(render_scale, render_scale), alpha=False).tobytes('png')
                    self.check(len(rendered))
                    visual = self.out / 'visuals' / sha(sid.encode()) / f'page-{number+1:06d}.png'
                    atomic(visual, rendered)
                    self.bytes_written += len(rendered)
                    visual_path = str(visual.relative_to(self.out))
                self.unit(sid, number, 'page', {'page': number + 1, 'text': text},
                          {'page': number + 1, 'ocr_status': ocr_status, 'raster_images': raster_images,
                           'vector_drawings': drawings, 'visual_artifact': visual_path,
                           'render_scale': render_scale, 'full_resolution_visual_pending': bool(render_scale is not None and render_scale < 1),
                           'ocr_languages': self.args.ocr_languages if self.args.pdf_ocr else None,
                           'visual_review_required': bool(visual_path), 'formula_validation_complete': False},
                          status='pending_ocr' if ocr_status.startswith('pending') else 'pending_visual_resolution' if render_scale is not None and render_scale < 1 else 'extracted')
                self.state(sid, cursor=max(row['cursor'], number + 1), status='extracting')
            pending = self.db.execute("SELECT count(*) FROM units WHERE source_id=? AND status!='extracted'", (sid,)).fetchone()[0]
            self.state(sid, status='partial_pending_page_capability' if pending else 'extracted',
                       notes={'pages_with_extraction_gap': pending, 'visual_and_formula_semantic_review': 'not_performed'})

    def csv_table(self, row, path):
        sid = row['source_id']
        count, batch, start = 0, [], 0
        with path.open('r', encoding=self.args.text_encoding or 'utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')
            header = next(reader, [])
            for values in reader:
                self.check()
                batch.append(values)
                if len(batch) >= self.args.table_rows:
                    if count >= row['cursor']:
                        self.unit(sid, count, 'rows', summarize_rows(header, batch),
                                  {'row_start': start, 'row_end_exclusive': start + len(batch), 'full_batch': True})
                        self.state(sid, cursor=count + 1, status='extracting', unit_kind='row_batches')
                    start += len(batch)
                    count += 1
                    batch = []
            if batch:
                if count >= row['cursor']:
                    self.unit(sid, count, 'rows', summarize_rows(header, batch),
                              {'row_start': start, 'row_end_exclusive': start + len(batch), 'full_batch': True})
                    self.state(sid, cursor=count + 1, status='extracting', unit_kind='row_batches')
                start += len(batch)
                count += 1
        self.state(sid, status='extracted', total_units=count, cursor=count, unit_kind='row_batches',
                   notes={'rows_audited': start, 'columns': header, 'sampling': False,
                          'scope': 'full-row structural and numeric audit; no model training or statistical inference'})

    def parquet(self, row, path):
        try:
            import pyarrow.parquet as pq
        except ImportError:
            self.state(row['source_id'], status='pending_pyarrow_dependency')
            return
        sid = row['source_id']
        parquet = pq.ParquetFile(path)
        count, rows = 0, 0
        for batch in parquet.iter_batches(batch_size=self.args.table_rows, use_threads=False):
            self.check()
            if count >= row['cursor']:
                columns = batch.to_pydict()
                stats = summarize_rows(list(columns), zip(*columns.values()))
                stats['schema'] = str(batch.schema)
                self.unit(sid, count, 'rows', stats, {'row_start': rows,
                          'row_end_exclusive': rows + batch.num_rows, 'full_batch': True})
                self.state(sid, cursor=count + 1, status='extracting', unit_kind='row_batches')
            count += 1
            rows += batch.num_rows
        self.state(sid, status='extracted', total_units=count, cursor=count, unit_kind='row_batches',
                   notes={'rows_audited': rows, 'schema': str(parquet.schema_arrow), 'sampling': False,
                          'scope': 'full-row structural and numeric audit; no model training or statistical inference'})

    def archive(self, row, path):
        sid = row['source_id']
        if row['archive_depth'] >= self.args.archive_depth:
            self.state(sid, status='pending_archive_depth_capacity', notes={'required_depth': row['archive_depth'] + 1})
            return
        suffix = path.suffix.lower()
        if suffix in ('.rar', '.7z', '.001', '.002'):
            self.state(sid, status='pending_archive_adapter', notes={'required': 'reviewed RAR/7z/multipart adapter; source retained'})
            return
        if suffix == '.zip':
            context = zipfile.ZipFile(path)
            members = context.infolist()
            def detail(m):
                mode = m.external_attr >> 16
                return m.filename, m.file_size, m.is_dir(), stat.S_ISLNK(mode), bool(m.flag_bits & 1)
            def opening(m):
                return context.open(m)
        elif tarfile.is_tarfile(path):
            context = tarfile.open(path)
            members = context.getmembers()
            def detail(m):
                return m.name, m.size, m.isdir(), not (m.isfile() or m.isdir()), False
            def opening(m):
                return context.extractfile(m)
        else:
            self.state(sid, status='pending_archive_adapter', notes={'required': 'standalone compression or unknown container adapter'})
            return
        expanded_root = self.out / 'members' / sha(sid.encode())
        expanded_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with context:
            self.state(sid, total_units=len(members), unit_kind='archive_members')
            for index in range(row['cursor'], len(members)):
                self.check()
                member = members[index]
                name, size, directory, unsafe_type, encrypted = detail(member)
                if directory:
                    self.unit(sid, index, 'archive_member', metadata={'name': name, 'directory': True})
                    self.state(sid, cursor=index + 1, status='extracting')
                    continue
                try:
                    safe_relative(name)
                    if unsafe_type or encrypted:
                        raise ValueError('nonregular_or_encrypted_member')
                except ValueError as exc:
                    self.unit(sid, index, 'archive_member', metadata={'name': name, 'failure': str(exc)}, status='blocked_member')
                    self.state(sid, cursor=index + 1, status='extracting')
                    continue
                if size > self.args.max_member_bytes:
                    self.state(sid, status='pending_member_capacity', notes={'member_index': index, 'member_bytes': size,
                               'required_max_member_bytes': size, 'source_retained': True})
                    return
                # Flat member filenames avoid duplicate names, traversal and link collisions.
                target_name = member_storage_name(index, name)
                target = expanded_root / target_name
                self.check(size)
                if not target.exists():
                    temporary = target.with_suffix(target.suffix + '.partial')
                    total = 0
                    try:
                        with opening(member) as source, temporary.open('wb') as dest:
                            while True:
                                self.check()
                                buf = source.read(min(1024 * 1024, size - total + 1))
                                if not buf:
                                    break
                                total += len(buf)
                                if total > size or total > self.args.max_member_bytes:
                                    raise ValueError('archive_member_size_mismatch')
                                dest.write(buf)
                        if total != size:
                            raise ValueError('archive_member_short_read')
                        temporary.chmod(0o600)
                        temporary.replace(target)
                    finally:
                        if temporary.exists():
                            temporary.unlink()
                    self.bytes_written += total
                elif target.stat().st_size != size:
                    raise ValueError('existing_member_size_mismatch')
                child_id = sha((sid + ':' + str(index) + ':' + name).encode())
                self.register({'source_id': child_id, 'relative_path': target_name, 'size_bytes': size},
                              root=expanded_root, parent=sid, depth=row['archive_depth'] + 1)
                self.unit(sid, index, 'archive_member', metadata={'name': name, 'size_bytes': size,
                          'child_id': child_id, 'child_source_relative_path': name})
                self.state(sid, cursor=index + 1, status='extracting')
        blocked = self.db.execute("SELECT count(*) FROM units WHERE source_id=? AND status!='extracted'", (sid,)).fetchone()[0]
        # Container expansion never means child content is learned.
        self.state(sid, status='container_expanded_with_gaps' if blocked else 'container_expanded',
                   notes={'blocked_members': blocked, 'children_require_separate_processing': True})

    def process(self, row):
        sid = row['source_id']
        path = self.verify(row)
        if path is None:
            return
        row = self.db.execute('SELECT * FROM sources WHERE source_id=?', (sid,)).fetchone()
        ext = path.suffix.lower()
        if not self.bind_protocol(row, ext):
            return
        if ext in TEXT:
            self.text(row, path)
        elif ext == '.pdf':
            self.pdf(row, path)
        elif ext in ('.csv', '.tsv'):
            self.csv_table(row, path)
        elif ext in ('.parquet', '.pq'):
            self.parquet(row, path)
        elif ext in ARCHIVES or ext in ('.001', '.002'):
            self.archive(row, path)
        elif ext in MEDIA:
            self.state(sid, status='pending_full_media_adapter', notes={'required': 'full-duration ASR plus visual review; no sampling completion'})
        elif ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            self.state(sid, status='pending_image_ocr_and_visual_review')
        elif ext in ('.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx'):
            self.state(sid, status='pending_full_office_adapter')
        elif ext in ('.h5', '.hdf5'):
            self.state(sid, status='pending_full_hdf5_adapter')
        elif ext in ('.downloading', '.part') or path.stat().st_size == 0:
            self.state(sid, status='blocked_incomplete_or_empty_source')
        else:
            self.state(sid, status='pending_format_review', notes={'extension': ext, 'source_retained': True})

    def export(self, reason):
        counts = {}
        destination = self.out / 'manifest-state.jsonl'
        with destination.with_suffix('.tmp').open('wb') as stream:
            for row in self.db.execute('SELECT * FROM sources ORDER BY source_id'):
                data = dict(row)
                data['notes'] = json.loads(data['notes'])
                data.update(semantic_read=False, synthesized=False, validated=False, learning_complete=False)
                protocol = self.db.execute('SELECT protocol FROM source_protocols WHERE source_id=?', (row['source_id'],)).fetchone()
                data['parser_protocol'] = json.loads(protocol['protocol']) if protocol else None
                counts[data['status']] = counts.get(data['status'], 0) + 1
                stream.write(canonical(data) + b'\n')
        destination.with_suffix('.tmp').chmod(0o600)
        destination.with_suffix('.tmp').replace(destination)
        unit_export = self.out / 'unit-state.jsonl'
        with unit_export.with_suffix('.tmp').open('wb') as stream:
            for row in self.db.execute('SELECT * FROM units ORDER BY source_id,unit_index'):
                unit = dict(row)
                unit['metadata'] = json.loads(unit['metadata'])
                stream.write(canonical(unit) + b'\n')
        unit_export.with_suffix('.tmp').chmod(0o600)
        unit_export.with_suffix('.tmp').replace(unit_export)
        result = {'format': VERSION, 'run_id': self.run_id, 'updated_utc': now(), 'stop_reason': reason,
                  'source_count_including_discovered_children': sum(counts.values()), 'states': counts,
                  'units_written_this_run': self.units_written, 'bytes_written_this_run': self.bytes_written,
                  'semantic_read_count_credited_by_worker': 0, 'strategy_evaluations': 0, 'actual_fills': 0,
                  'global_learning_percent': None, 'plaintext_publication': False}
        atomic(self.out / 'coverage.json', canonical(result) + b'\n')
        self.db.execute('UPDATE runs SET finished_utc=?, result=? WHERE run_id=?', (now(), canonical(result).decode(), self.run_id))
        self.db.commit()
        return result

    def run(self):
        manifest = Path(self.args.manifest)
        with manifest.open() as source:
            if manifest.suffix.lower() == '.json':
                data = json.load(source)
                entries = data if isinstance(data, list) else data.get('files', data.get('entries', []))
                for entry in entries:
                    self.register(entry)
            else:
                for line in source:
                    if line.strip():
                        self.register(json.loads(line))
        reason, visited, processed = 'queue_pass_complete', set(), 0
        try:
            while processed < self.args.max_files:
                self.check()
                selection = "status NOT IN ('extracted','duplicate_linked','container_expanded','container_expanded_with_gaps')" if self.args.retry_pending else "status IN ('inventoried','transferred','extracting','pending_transfer')"
                rows = self.db.execute('SELECT * FROM sources WHERE ' + selection + ' ORDER BY archive_depth, source_id').fetchall()
                row = next((r for r in rows if r['source_id'] not in visited), None)
                if row is None:
                    break
                visited.add(row['source_id'])
                try:
                    source_path = contained_file(Path(row['source_root']), row['relative_path'])
                    if not source_path.is_file():
                        self.state(row['source_id'], status='pending_transfer')
                        continue
                    processed += 1
                    self.process(row)
                except BudgetStop:
                    raise
                except UnicodeError as exc:
                    self.state(row['source_id'], status='pending_text_encoding', notes={'error_class': type(exc).__name__})
                except MemoryError:
                    self.state(row['source_id'], status='pending_memory_capacity', notes={'max_memory_mib': self.args.max_memory_mib, 'source_retained': True})
                except csv.Error as exc:
                    self.state(row['source_id'], status='pending_csv_format_or_field_capacity', notes={'error_class': type(exc).__name__, 'source_retained': True})
                except Exception as exc:
                    # Do not echo untrusted source content or raw exception text to logs.
                    self.state(row['source_id'], status='processing_error', notes={'error_class': type(exc).__name__})
            if processed >= self.args.max_files:
                reason = 'file_budget'
        except BudgetStop as exc:
            reason = str(exc)
        return self.export(reason)


def summarize_rows(header, rows):
    columns = [{'column': str(name), 'null_or_empty': 0, 'numeric_count': 0, 'nonfinite_count': 0,
                'minimum': None, 'maximum': None} for name in header]
    count = width_errors = 0
    for values in rows:
        count += 1
        if len(values) != len(columns):
            width_errors += 1
        for index, item in enumerate(columns):
            value = values[index] if index < len(values) else None
            if value is None or value == '':
                item['null_or_empty'] += 1
                continue
            try:
                number = float(value)
            except (ValueError, TypeError, OverflowError):
                continue
            if not math.isfinite(number):
                item['nonfinite_count'] += 1
                continue
            item['numeric_count'] += 1
            item['minimum'] = number if item['minimum'] is None else min(number, item['minimum'])
            item['maximum'] = number if item['maximum'] is None else max(number, item['maximum'])
    return {'rows': count, 'width_errors': width_errors, 'columns': columns, 'sampling': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--input-root', required=True)
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--preflight', required=True)
    parser.add_argument('--backend', choices=('github-actions', 'aws'), required=True)
    parser.add_argument('--max-files', type=int, default=20)
    parser.add_argument('--max-units', type=int, default=200)
    parser.add_argument('--max-seconds', type=int, default=1800)
    parser.add_argument('--max-output-bytes', type=int, default=1024**3)
    parser.add_argument('--max-member-bytes', type=int, default=1024**3)
    parser.add_argument('--archive-depth', type=int, default=8)
    parser.add_argument('--text-chunk-chars', type=int, default=24000)
    parser.add_argument('--text-encoding')
    parser.add_argument('--table-rows', type=int, default=10000)
    parser.add_argument('--pdf-ocr', action='store_true')
    parser.add_argument('--ocr-languages', default='eng+chi_sim')
    parser.add_argument('--max-page-pixels', type=int, default=8000000)
    parser.add_argument('--max-memory-mib', type=int, default=2048)
    parser.add_argument('--retry-pending', action='store_true', help='Retry explicit dependency/capacity/format failures after their cause has been repaired')
    args = parser.parse_args()
    if min(args.max_files, args.max_units, args.max_seconds, args.max_output_bytes,
           args.max_member_bytes, args.archive_depth, args.text_chunk_chars, args.table_rows,
           args.max_page_pixels, args.max_memory_mib) <= 0:
        parser.error('all resource limits must be positive')
    os.umask(0o077)
    preflight = json.loads(Path(args.preflight).read_text())
    cloud_guard(args.backend, preflight)
    # Cloud-only process ceiling catches decompressor/parser allocations that
    # cannot expose their final size before parsing. Never set on the laptop.
    import resource
    ceiling = args.max_memory_mib * 1024 * 1024
    previous_soft, previous_hard = resource.getrlimit(resource.RLIMIT_AS)
    finite = [value for value in (ceiling, previous_soft, previous_hard) if value != resource.RLIM_INFINITY]
    ceiling = min(finite)
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    output = Path(args.output_root)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = output / 'worker.lock'
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump({'pid': os.getpid(), 'created_utc': now()}, handle)
    worker = None
    try:
        worker = Worker(args, preflight)
        result = worker.run()
        print(json.dumps({'status': 'checkpointed', 'source_count': result['source_count_including_discovered_children'],
                          'units_written': result['units_written_this_run'], 'stop_reason': result['stop_reason']}))
    finally:
        if worker:
            worker.db.close()
        lock.unlink()


if __name__ == '__main__':
    main()
