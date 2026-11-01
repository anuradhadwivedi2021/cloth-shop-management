import json
from datetime import datetime, timedelta

import pytest

from core import keystore
from core.keystore import KeystoreError, KeystoreLocked

T0 = datetime(2026, 11, 1, 10, 0, 0)


@pytest.fixture
def ks(tmp_path):
    path = tmp_path / "shop.keys"
    key, recovery = keystore.create_keystore(path, "Anu", "owner-password")
    return path, key, recovery


def test_password_and_recovery_key_unlock_the_same_database_key(ks):
    path, key, recovery = ks
    assert len(key) == 64
    assert keystore.unlock_keystore(path, "anu", "owner-password") == key
    assert keystore.unlock_keystore(path, "ANU", "owner-password") == key  # slot names ignore case
    assert keystore.unlock_keystore(path, "recovery", recovery) == key
    assert keystore.unlock_keystore(path, "recovery", recovery.lower().replace("-", " ")) == key


def test_recovery_key_format(ks):
    _, _, recovery = ks
    groups = recovery.split("-")
    assert len(groups) == 6 and all(len(g) == 4 and g.isalnum() for g in groups)
    assert keystore.new_recovery_key() != keystore.new_recovery_key()


def test_wrong_secrets_fail_with_clear_but_safe_messages(ks):
    path, _, _ = ks
    with pytest.raises(KeystoreError, match="Wrong username or password"):
        keystore.unlock_keystore(path, "anu", "wrong-password")
    with pytest.raises(KeystoreError, match="Wrong username or password"):
        keystore.unlock_keystore(path, "ghost", "owner-password")  # unknown user: same message
    with pytest.raises(KeystoreError, match="Wrong recovery key"):
        keystore.unlock_keystore(path, "recovery", "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF")


def test_key_is_not_stored_in_the_clear(ks):
    path, key, recovery = ks
    text = path.read_text()
    assert key not in text and "owner-password" not in text and recovery.replace("-", "") not in text


def test_wrong_attempts_lock_the_slot(ks):
    path, key, _ = ks
    for i in range(5):
        with pytest.raises(KeystoreError):
            keystore.unlock_keystore(path, "anu", "nope", now=T0 + timedelta(seconds=i))
    with pytest.raises(KeystoreLocked):
        keystore.unlock_keystore(path, "anu", "owner-password", now=T0 + timedelta(seconds=10))
    assert keystore.unlock_keystore(path, "anu", "owner-password", now=T0 + timedelta(seconds=40)) == key


def test_add_replace_and_remove_user_slots(ks):
    path, key, _ = ks
    keystore.set_slot(path, key, "till", "till-password")
    assert keystore.unlock_keystore(path, "till", "till-password") == key
    keystore.set_slot(path, key, "till", "new-password")
    with pytest.raises(KeystoreError):
        keystore.unlock_keystore(path, "till", "till-password")
    assert keystore.unlock_keystore(path, "till", "new-password") == key
    assert keystore.slot_names(path) == ["anu", "till"]
    keystore.remove_slot(path, "till")
    with pytest.raises(KeystoreError):
        keystore.unlock_keystore(path, "till", "new-password")


def test_recovery_slot_is_protected_and_can_be_rotated(ks):
    path, key, old_recovery = ks
    with pytest.raises(KeystoreError):
        keystore.remove_slot(path, "recovery")
    with pytest.raises(KeystoreError):
        keystore.set_slot(path, key, "recovery", "some-password")
    new_recovery = keystore.rotate_recovery_key(path, key)
    assert new_recovery != old_recovery
    assert keystore.unlock_keystore(path, "recovery", new_recovery) == key
    with pytest.raises(KeystoreError):
        keystore.unlock_keystore(path, "recovery", old_recovery)


def test_existing_or_damaged_key_file(ks, tmp_path):
    path, _, _ = ks
    with pytest.raises(KeystoreError, match="already exists"):
        keystore.create_keystore(path, "x", "another-pass")
    path.write_text("{not json")
    with pytest.raises(KeystoreError, match="missing or damaged"):
        keystore.unlock_keystore(path, "anu", "owner-password")
    with pytest.raises(KeystoreError, match="missing or damaged"):
        keystore.unlock_keystore(tmp_path / "nothing.keys", "anu", "x")


def test_tampering_with_a_slot_is_detected(ks):
    path, key, _ = ks
    data = json.loads(path.read_text())
    data["slots"]["till"] = data["slots"]["anu"]  # copy one user's slot under another name
    path.write_text(json.dumps(data))
    with pytest.raises(KeystoreError):
        keystore.unlock_keystore(path, "till", "owner-password")  # slot name is part of the check
