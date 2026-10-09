"""Isolated core tests: intentionally do not import Flet or touch user's home."""
import ast
import os
import json
import uuid
import time
import sqlite3
import secrets
import string
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'app' / 'main.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
node_names = {'derive_key','encrypt','decrypt','generate_password','validate_url','analyze_passwords'}
nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in node_names or isinstance(n, ast.ClassDef) and n.name == 'VaultDB']

@pytest.fixture
def vault(tmp_path):
    ns = dict(os=os,json=json,uuid=uuid,time=time,sqlite3=sqlite3,secrets=secrets,string=string,tempfile=tempfile,threading=threading,Path=Path,datetime=datetime,Counter=Counter,urlparse=urlparse,Type=Type,hash_secret_raw=hash_secret_raw,AESGCM=AESGCM,APP_DIR=tmp_path, CHECK_TEXT=b'PasswordVault verification v1',AAD_CHECK=b'vault-check-v1',AAD_ENTRY=b'vault-entry-v1:',AAD_PROFILE=b'manpass-profile-v1')
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(SOURCE),'exec'),ns)
    db = ns['VaultDB'](tmp_path / 'vault.db')
    yield db, ns
    try: db.close()
    except Exception: pass

def test_password_unlock_and_rejection(vault):
    db,ns=vault
    key=db.setup('very long unique master password')
    assert db.unlock('very long unique master password') == key
    with pytest.raises(ValueError): db.unlock('incorrect master password')

def test_entry_profile_and_reopen(vault):
    db,ns=vault
    key=db.setup('very long unique master password')
    db.save_entry(key,{'site':'Test','url':'https://example.org','login':'user','password':'verystrong','two_factor':True})
    db.save_profile(key,name='Alex')
    db.save_profile(key,email='test@example.org',phone='+123456789')
    assert db.profile(key)['name']=='Alex'
    assert db.profile(key)['phone']=='+123456789'
    assert db.entries(key)[0][1]['url']=='https://example.org'

def test_integrity_tamper_detection(vault):
    db,ns=vault
    key=db.setup('very long unique master password')
    db.save_entry(key,{'site':'Test','password':'strong'})
    item_id,blob=db.db.execute('SELECT id, encrypted FROM entries').fetchone()
    corrupted=bytearray(blob);corrupted[-1]^=1
    with db.db: db.db.execute('UPDATE entries SET encrypted=? WHERE id=?',(bytes(corrupted),item_id))
    with pytest.raises(Exception): db.entries(key)

def test_backup_and_master_change(vault):
    db,ns=vault
    key=db.setup('very long unique master password')
    db.save_entry(key,{'site':'Test','password':'strong'})
    db.save_profile(key,name='Alex',email='hello@example.org')
    backup=db.backup()
    assert backup.exists()
    newkey,older=db.change_master('very long unique master password','a much newer long master password')
    assert db.entries(newkey)[0][1]['site']=='Test'
    assert db.profile(newkey)['email']=='hello@example.org'
    with pytest.raises(ValueError): db.unlock('very long unique master password')
    restored=ns['VaultDB'](backup)
    try: assert restored.entries(key)[0][1]['site']=='Test'
    finally: restored.close()
