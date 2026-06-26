/* labkit — zero-setup platform layer for the C labs.
 *
 * Postgres via libpq; Redis via a tiny built-in RESP client (no extra deps).
 * Connections come from DATABASE_URL / REDIS_URL.
 *
 * Compile a lab with:  gcc lab.c labkit.c -I<this dir> -lpq -o lab
 */
#ifndef LABKIT_H
#define LABKIT_H

#include <libpq-fe.h>

#ifdef __cplusplus
extern "C" {
#endif

void lk_init(void);

/* Postgres. params is an array of nparams C strings (or NULL). Caller PQclear()s
 * the returned result. Both helpers increment the query counter. */
PGresult *lk_query(const char *sql, int nparams, const char *const *params);
int lk_exec(const char *sql, int nparams, const char *const *params);
int lk_query_count(void);
void lk_reset_counters(void);

/* Redis. lk_cache_get returns a malloc'd string the caller frees, or NULL on miss. */
char *lk_cache_get(const char *key);
void lk_cache_set(const char *key, const char *val, int ttl_seconds);
void lk_cache_del(const char *key);
int lk_cache_exists(const char *key);
void lk_cache_flush(void);

#ifdef __cplusplus
}
#endif

#endif
