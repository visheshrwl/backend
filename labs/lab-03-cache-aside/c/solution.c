/* Lab 03: Cache-Aside with Postgres + Redis — C reference solution.
 *
 * labkit gives you ready Postgres (lk_query/lk_exec) and Redis (lk_cache_*)
 * helpers — no setup. The cached user is stored as "id|name|email|plan".
 *
 *   Run:  gcc solution.c ../../../tooling/c/labkit.c -I../../../tooling/c -lpq -lpthread -o /tmp/lab03c && /tmp/lab03c
 */
#include "labkit.h"

#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TTL 60

typedef struct {
    int found;
    int id;
    char name[128];
    char email[128];
    char plan[64];
} User;

static void user_key(int id, char *buf, int cap) { snprintf(buf, cap, "user:%d", id); }

static void serialize(const User *u, char *buf, int cap) {
    snprintf(buf, cap, "%d|%s|%s|%s", u->id, u->name, u->email, u->plan);
}

static void deserialize(const char *s, User *u) {
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s", s);
    char *save = NULL;
    u->id = atoi(strtok_r(tmp, "|", &save));
    snprintf(u->name, sizeof(u->name), "%s", strtok_r(NULL, "|", &save));
    snprintf(u->email, sizeof(u->email), "%s", strtok_r(NULL, "|", &save));
    snprintf(u->plan, sizeof(u->plan), "%s", strtok_r(NULL, "|", &save));
    u->found = 1;
}

/* Part 1 — cache-aside read. Returns 1 if found, 0 otherwise. */
int get_user_profile(int id, User *out) {
    char key[32];
    user_key(id, key, sizeof(key));

    char *cached = lk_cache_get(key);
    if (cached) {
        deserialize(cached, out);
        free(cached);
        return 1;
    }

    char idbuf[16];
    snprintf(idbuf, sizeof(idbuf), "%d", id);
    const char *params[] = {idbuf};
    PGresult *res = lk_query("SELECT id, name, email, plan FROM users WHERE id = $1", 1, params);
    if (PQntuples(res) == 0) { PQclear(res); return 0; }

    out->found = 1;
    out->id = atoi(PQgetvalue(res, 0, 0));
    snprintf(out->name, sizeof(out->name), "%s", PQgetvalue(res, 0, 1));
    snprintf(out->email, sizeof(out->email), "%s", PQgetvalue(res, 0, 2));
    snprintf(out->plan, sizeof(out->plan), "%s", PQgetvalue(res, 0, 3));
    PQclear(res);

    char ser[512];
    serialize(out, ser, sizeof(ser));
    lk_cache_set(key, ser, TTL);
    return 1;
}

void update_user_plan(int id, const char *plan) {
    char idbuf[16];
    snprintf(idbuf, sizeof(idbuf), "%d", id);
    const char *params[] = {plan, idbuf};
    lk_exec("UPDATE users SET plan = $1, updated_at = now() WHERE id = $2", 2, params);
    char key[32];
    user_key(id, key, sizeof(key));
    lk_cache_del(key);
}

/* Part 2 — single-flight: one lock per key + double-check. */
#define MAXLOCKS 64
static pthread_mutex_t reg_guard = PTHREAD_MUTEX_INITIALIZER;
static char lock_keys[MAXLOCKS][64];
static pthread_mutex_t lock_muts[MAXLOCKS];
static int lock_n = 0;

static pthread_mutex_t *key_lock(const char *key) {
    pthread_mutex_lock(&reg_guard);
    for (int i = 0; i < lock_n; i++) {
        if (strcmp(lock_keys[i], key) == 0) { pthread_mutex_unlock(&reg_guard); return &lock_muts[i]; }
    }
    snprintf(lock_keys[lock_n], 64, "%s", key);
    pthread_mutex_init(&lock_muts[lock_n], NULL);
    pthread_mutex_t *m = &lock_muts[lock_n];
    lock_n++;
    pthread_mutex_unlock(&reg_guard);
    return m;
}

int get_user_profile_singleflight(int id, User *out) {
    char key[32];
    user_key(id, key, sizeof(key));

    char *cached = lk_cache_get(key);
    if (cached) { deserialize(cached, out); free(cached); return 1; }

    pthread_mutex_t *lock = key_lock(key);
    pthread_mutex_lock(lock);

    cached = lk_cache_get(key); /* double-check */
    if (cached) { deserialize(cached, out); free(cached); pthread_mutex_unlock(lock); return 1; }

    int ok = get_user_profile(id, out); /* the cold path: queries once, populates */
    pthread_mutex_unlock(lock);
    return ok;
}

static int g_id = 1;
static void *worker(void *arg) {
    (void)arg;
    User u;
    get_user_profile_singleflight(g_id, &u);
    return NULL;
}

int main(void) {
    lk_init();

    lk_exec("UPDATE users SET plan = 'pro' WHERE id = 1", 0, NULL);
    lk_cache_flush();
    lk_reset_counters();

    User u;
    assert(get_user_profile(1, &u) && u.id == 1 && "user 1 should be fetched");
    assert(lk_query_count() >= 1 && "first read should hit Postgres");
    assert(lk_cache_exists("user:1") && "first read should populate the cache");

    lk_reset_counters();
    assert(get_user_profile(1, &u) && u.id == 1);
    assert(lk_query_count() == 0 && "a cache hit must not query Postgres");

    update_user_plan(1, "enterprise");
    assert(!lk_cache_exists("user:1") && "update must invalidate the cache");
    assert(get_user_profile(1, &u) && strcmp(u.plan, "enterprise") == 0 && "refilled with new plan");

    assert(!get_user_profile(999999, &u) && "missing user returns 0");

    lk_cache_flush();
    lk_reset_counters();
    pthread_t threads[50];
    for (int i = 0; i < 50; i++) pthread_create(&threads[i], NULL, worker, NULL);
    for (int i = 0; i < 50; i++) pthread_join(threads[i], NULL);
    assert(lk_query_count() == 1 && "single-flight: only one DB query for 50 concurrent reads");

    printf("OK — cache-aside, invalidation, and single-flight (1 DB query for 50 concurrent reads)\n");
    return 0;
}
