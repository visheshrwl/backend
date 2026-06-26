# labkit (C / C++)

Zero-setup Postgres + Redis platform shim for the backend labs. C and C++ have
no universal package registry, so this is a **vendored** unit: copy `labkit.h`
and `labkit.c` into each lab's `c/` and `cpp/` folders.

Postgres uses libpq; Redis uses a tiny built-in RESP client (no hiredis needed).
Connections come from `DATABASE_URL` / `REDIS_URL`.

```c
#include "labkit.h"
lk_init();
PGresult *r = lk_query("SELECT id FROM users WHERE id = $1", 1, (const char*[]){"1"});
char *cached = lk_cache_get("user:1");
```

## Compile

```bash
# C
gcc lab.c labkit.c -I. -I$(pg_config --includedir) -lpq -lpthread -o lab

# C++ (compile labkit.c with gcc, link with g++)
gcc -c labkit.c -I. -I$(pg_config --includedir) -o labkit.o
g++ -std=c++20 lab.cpp labkit.o -I. -I$(pg_config --includedir) -lpq -lpthread -o lab
```

## API

`lk_init`, `lk_query`, `lk_exec`, `lk_query_count`, `lk_reset_counters`,
`lk_cache_get/set/del/exists/flush`.
