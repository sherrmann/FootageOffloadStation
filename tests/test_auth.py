import pytest

from footage_offload import auth


def test_hash_format_and_verify():
    h = auth.hash_password("correct horse")
    parts = h.split("$")
    assert parts[:4] == ["scrypt", str(2 ** 14), "8", "1"]
    assert len(parts) == 6
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("correct horsE", h)
    assert not auth.verify_password("", h)


def test_salt_differs():
    assert auth.hash_password("abcdefgh") != auth.hash_password("abcdefgh")


def test_too_short():
    with pytest.raises(auth.PasswordError) as e:
        auth.hash_password("1234567")
    assert e.value.message_id == "error.password_too_short"
    assert auth.password_problem("12345678") is None
    assert auth.password_problem(12345678) == "error.type_string"


@pytest.mark.parametrize("stored", [
    "", "plain", "scrypt$16384$8$1$!!$!!", "bcrypt$1$2$3$AAAA$AAAA",
    "scrypt$1000$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAA",          # n not a power of two
    "scrypt$1073741824$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAA",    # absurd memory
])
def test_verify_rejects_broken_hashes(stored):
    assert not auth.verify_password("whatever1", stored)


def test_verify_non_string():
    h = auth.hash_password("abcdefgh")
    assert not auth.verify_password(None, h)
    assert not auth.verify_password("abcdefgh", None)


def test_csrf():
    t = auth.new_csrf_token()
    assert len(t) >= 32
    assert auth.csrf_ok(t, t)
    assert not auth.csrf_ok(t, t + "x")
    assert not auth.csrf_ok(t, None)
    assert not auth.csrf_ok("", "")


def test_password_tag_changes_with_hash():
    assert auth.password_tag("a") != auth.password_tag("b")
    assert auth.password_tag("a") == auth.password_tag("a")


def test_limiter_locks_out_and_recovers():
    now = [1000.0]
    lim = auth.LoginLimiter(free_attempts=3, base_lockout_s=10, max_lockout_s=60,
                            forget_after_s=100, clock=lambda: now[0])
    for _ in range(2):
        lim.failure("1.2.3.4")
        assert lim.retry_after("1.2.3.4") == 0
    lim.failure("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 10
    assert lim.retry_after("5.6.7.8") == 0          # other clients unaffected
    now[0] += 10
    assert lim.retry_after("1.2.3.4") == 0
    lim.failure("1.2.3.4")                          # doubles
    assert lim.retry_after("1.2.3.4") == 20
    for _ in range(10):
        lim.failure("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 60         # capped
    lim.success("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 0


def test_limiter_forgets_old_failures():
    now = [0.0]
    lim = auth.LoginLimiter(free_attempts=2, base_lockout_s=5, forget_after_s=50, clock=lambda: now[0])
    lim.failure("a")
    now[0] += 51
    lim.failure("a")
    assert lim.retry_after("a") == 0
