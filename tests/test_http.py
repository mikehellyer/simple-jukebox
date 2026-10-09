import ssl

from simple_jukebox.core.http import SSL_CONTEXT


def test_ssl_context_is_a_real_ssl_context():
    assert isinstance(SSL_CONTEXT, ssl.SSLContext)


def test_ssl_context_has_certificates_loaded():
    # cert_store_stats() only returns non-zero counts if a CA bundle was
    # actually loaded — this is what guards against a future refactor
    # accidentally dropping the cafile= argument.
    stats = SSL_CONTEXT.cert_store_stats()
    assert stats["x509_ca"] > 0
