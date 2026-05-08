# Inject Windows / system certificate store into SSL context
# 解決 corporate firewall / antivirus MITM cert intercept 嘅問題
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
