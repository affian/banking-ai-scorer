import sys, time, json, urllib.request
u = sys.argv[1].rstrip("/"); n = 10; ids = [5001, 5002, 5014, 5025]; lat = []; ok = 0
path = sys.argv[2] if len(sys.argv) > 2 else "score-account"
method = "POST" if path == "score-and-store" else "GET"
t0 = time.perf_counter()
for i in range(n):
    t = time.perf_counter()
    try:
        req = urllib.request.Request("%s/%s/%d" % (u, path, ids[i % 4]), method=method)
        json.load(urllib.request.urlopen(req, timeout=30)); ok += 1
    except Exception:
        pass
    lat.append((time.perf_counter() - t) * 1000)
d = time.perf_counter() - t0
lat.sort()
p = lambda q: lat[max(0, -(-q * len(lat) // 100) - 1)]
print("=" * 48)
print("=== Code Engine Live Throughput & Latency (Oracle, %s) ==" % path)
print("=" * 48)
print("Total Completed Requests : %d / %d" % (ok, n))
print("Total Duration           : %.2f seconds" % d)
print("Throughput               : %.2f req/sec" % (n / d))
print("p50 Latency (Median)     : %.2f ms" % p(50))
print("p95 Latency (SLA)        : %.2f ms" % p(95))
print("p99 Latency (Tail)       : %.2f ms" % p(99))
print("=" * 48)
