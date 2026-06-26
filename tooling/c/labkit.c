/* labkit implementation: libpq for Postgres, a minimal RESP client for Redis. */
#include "labkit.h"

#include <netdb.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static PGconn *g_conn = NULL;
static int g_query_count = 0;
static int g_redis_fd = -1;
static pthread_mutex_t g_pg_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_redis_mu = PTHREAD_MUTEX_INITIALIZER;

/* ── Postgres ── */

static const char *db_url(void) {
    const char *u = getenv("DATABASE_URL");
    return u ? u : "postgres://labs:labs@localhost:5432/labs";
}

PGresult *lk_query(const char *sql, int nparams, const char *const *params) {
    pthread_mutex_lock(&g_pg_mu);
    g_query_count++;
    PGresult *res = PQexecParams(g_conn, sql, nparams, NULL, params, NULL, NULL, 0);
    pthread_mutex_unlock(&g_pg_mu);
    if (PQresultStatus(res) != PGRES_TUPLES_OK && PQresultStatus(res) != PGRES_COMMAND_OK) {
        fprintf(stderr, "query failed: %s\n", PQerrorMessage(g_conn));
        exit(1);
    }
    return res;
}

int lk_exec(const char *sql, int nparams, const char *const *params) {
    pthread_mutex_lock(&g_pg_mu);
    g_query_count++;
    PGresult *res = PQexecParams(g_conn, sql, nparams, NULL, params, NULL, NULL, 0);
    pthread_mutex_unlock(&g_pg_mu);
    if (PQresultStatus(res) != PGRES_COMMAND_OK && PQresultStatus(res) != PGRES_TUPLES_OK) {
        fprintf(stderr, "exec failed: %s\n", PQerrorMessage(g_conn));
        exit(1);
    }
    int n = atoi(PQcmdTuples(res));
    PQclear(res);
    return n;
}

int lk_query_count(void) { return g_query_count; }
void lk_reset_counters(void) { g_query_count = 0; }

/* ── Redis (minimal RESP over a TCP socket) ── */

static void parse_redis_url(char *host, int hostlen, int *port) {
    const char *u = getenv("REDIS_URL");
    snprintf(host, hostlen, "localhost");
    *port = 6379;
    if (!u) return;
    const char *p = strstr(u, "://");
    p = p ? p + 3 : u;
    const char *colon = strchr(p, ':');
    if (colon) {
        int len = (int)(colon - p);
        if (len < hostlen) { memcpy(host, p, len); host[len] = '\0'; }
        *port = atoi(colon + 1);
    }
}

static int redis_connect(void) {
    char host[256];
    int port;
    parse_redis_url(host, sizeof(host), &port);
    char portstr[16];
    snprintf(portstr, sizeof(portstr), "%d", port);

    struct addrinfo hints = {0}, *res;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, portstr, &hints, &res) != 0) return -1;
    int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (fd >= 0 && connect(fd, res->ai_addr, res->ai_addrlen) != 0) { close(fd); fd = -1; }
    freeaddrinfo(res);
    return fd;
}

/* Send a command as a RESP array of bulk strings. */
static void redis_send(int argc, const char *const *argv) {
    char buf[8192];
    int n = snprintf(buf, sizeof(buf), "*%d\r\n", argc);
    for (int i = 0; i < argc; i++) {
        n += snprintf(buf + n, sizeof(buf) - n, "$%zu\r\n%s\r\n", strlen(argv[i]), argv[i]);
    }
    if (write(g_redis_fd, buf, n) < 0) { perror("redis write"); exit(1); }
}

/* Read one line (up to \r\n) into buf. */
static int redis_read_line(char *buf, int cap) {
    int i = 0;
    char c;
    while (i < cap - 1 && read(g_redis_fd, &c, 1) == 1) {
        if (c == '\r') { read(g_redis_fd, &c, 1); break; } /* consume \n */
        buf[i++] = c;
    }
    buf[i] = '\0';
    return i;
}

/* Read a full reply. For bulk strings returns a malloc'd payload (out). For
 * integers returns the value via *intval. Returns the RESP type byte. */
static char redis_read_reply(char **out, long *intval) {
    char head[64];
    char c;
    if (read(g_redis_fd, &c, 1) != 1) return 0;
    redis_read_line(head, sizeof(head));
    if (out) *out = NULL;
    if (intval) *intval = 0;
    if (c == ':') {
        if (intval) *intval = atol(head);
    } else if (c == '$') {
        long len = atol(head);
        if (len < 0) return c; /* nil */
        char *payload = malloc(len + 1);
        long got = 0;
        while (got < len) {
            long r = read(g_redis_fd, payload + got, len - got);
            if (r <= 0) break;
            got += r;
        }
        payload[len] = '\0';
        char crlf[2];
        read(g_redis_fd, crlf, 2); /* trailing \r\n */
        if (out) *out = payload; else free(payload);
    }
    return c;
}

char *lk_cache_get(const char *key) {
    pthread_mutex_lock(&g_redis_mu);
    const char *argv[] = {"GET", key};
    redis_send(2, argv);
    char *out = NULL;
    redis_read_reply(&out, NULL);
    pthread_mutex_unlock(&g_redis_mu);
    return out; /* NULL on miss */
}

void lk_cache_set(const char *key, const char *val, int ttl_seconds) {
    pthread_mutex_lock(&g_redis_mu);
    if (ttl_seconds > 0) {
        char ttl[16];
        snprintf(ttl, sizeof(ttl), "%d", ttl_seconds);
        const char *argv[] = {"SET", key, val, "EX", ttl};
        redis_send(5, argv);
    } else {
        const char *argv[] = {"SET", key, val};
        redis_send(3, argv);
    }
    char line[64];
    char c;
    read(g_redis_fd, &c, 1);
    redis_read_line(line, sizeof(line)); /* +OK */
    pthread_mutex_unlock(&g_redis_mu);
}

void lk_cache_del(const char *key) {
    pthread_mutex_lock(&g_redis_mu);
    const char *argv[] = {"DEL", key};
    redis_send(2, argv);
    long n;
    redis_read_reply(NULL, &n);
    pthread_mutex_unlock(&g_redis_mu);
}

int lk_cache_exists(const char *key) {
    pthread_mutex_lock(&g_redis_mu);
    const char *argv[] = {"EXISTS", key};
    redis_send(2, argv);
    long n = 0;
    redis_read_reply(NULL, &n);
    pthread_mutex_unlock(&g_redis_mu);
    return n > 0;
}

void lk_cache_flush(void) {
    pthread_mutex_lock(&g_redis_mu);
    const char *argv[] = {"FLUSHDB"};
    redis_send(1, argv);
    char line[64];
    char c;
    read(g_redis_fd, &c, 1);
    redis_read_line(line, sizeof(line));
    pthread_mutex_unlock(&g_redis_mu);
}

/* ── init ── */

void lk_init(void) {
    g_conn = PQconnectdb(db_url());
    if (PQstatus(g_conn) != CONNECTION_OK) {
        fprintf(stderr, "postgres connect failed: %s\n", PQerrorMessage(g_conn));
        exit(1);
    }
    g_redis_fd = redis_connect();
    if (g_redis_fd < 0) {
        fprintf(stderr, "redis connect failed\n");
        exit(1);
    }
}
