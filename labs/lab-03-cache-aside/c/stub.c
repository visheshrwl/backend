/* Lab 03: Cache-Aside with Postgres + Redis — C, YOUR TURN.
 *
 *   Run / validate:  gcc stub.c ../../../tooling/c/labkit.c -I../../../tooling/c -lpq -lpthread -o /tmp/lab03c && /tmp/lab03c
 *   Reference:        gcc solution.c ../../../tooling/c/labkit.c -I../../../tooling/c -lpq -lpthread -o /tmp/lab03c && /tmp/lab03c
 *
 * labkit API: lk_query(sql, nparams, params) -> PGresult* (PQntuples/PQgetvalue),
 *             lk_exec(sql, nparams, params), lk_query_count(),
 *             lk_cache_get(key) -> malloc'd or NULL, lk_cache_set(key, val, ttl),
 *             lk_cache_del(key), lk_cache_exists(key).
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

/* TODO: cache-aside read. lk_cache_get(key) first (deserialize, free, return 1).
 * On a miss, lk_query("SELECT id, name, email, plan FROM users WHERE id = $1").
 * If 0 rows return 0; else fill *out from PQgetvalue, serialize, lk_cache_set,
 * return 1. */
int get_user_profile(int id, User *out) {
    (void)id; (void)out;
    assert(0 && "TODO: implement get_user_profile");
    return 0;
}

/* TODO: lk_exec UPDATE users SET plan=$1, updated_at=now() WHERE id=$2; then
 * lk_cache_del(user_key). */
void update_user_plan(int id, const char *plan) {
    (void)id; (void)plan;
    assert(0 && "TODO: implement update_user_plan");
}

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

/* TODO: stampede-safe read. lk_cache_get; on a miss take key_lock(key),
 * DOUBLE-CHECK the cache, then call get_user_profile (the cold path) once. */
int get_user_profile_singleflight(int id, User *out) {
    (void)id; (void)out; (void)key_lock;
    assert(0 && "TODO: implement get_user_profile_singleflight");
    return 0;
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
