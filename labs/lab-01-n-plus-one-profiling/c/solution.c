/* Lab 01: N+1 Query Profiling — C reference solution (PostgreSQL).
 *
 * Runs against real Postgres through labkit; lk_query_count() proves 101->1->2.
 * Each fetch returns {users, posts} so the data is checked too. Creates and
 * seeds its own tables (n1_users, n1_posts).
 *
 *   Run:  gcc solution.c ../../../tooling/c/labkit.c -I../../../tooling/c -lpq -lpthread -o /tmp/lab01c && /tmp/lab01c
 */
#include "labkit.h"

#include <assert.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define USER_COUNT 100
#define POSTS_PER_USER 5

typedef struct {
    int users;
    int posts;
} Result;

static void setup_dataset(void) {
    lk_exec("DROP TABLE IF EXISTS n1_posts", 0, NULL);
    lk_exec("DROP TABLE IF EXISTS n1_users", 0, NULL);
    lk_exec("CREATE TABLE n1_users (id INT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)", 0, NULL);
    lk_exec("CREATE TABLE n1_posts (id INT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, user_id INT NOT NULL REFERENCES n1_users(id))", 0, NULL);
    lk_exec("CREATE INDEX idx_n1_posts_user_id ON n1_posts(user_id)", 0, NULL);
    lk_exec("INSERT INTO n1_users SELECT g, 'User ' || g, 'user' || g || '@example.com' FROM generate_series(1, 100) g", 0, NULL);
    lk_exec("INSERT INTO n1_posts SELECT u*10+p, 'Post ' || p, 'body', u FROM generate_series(1, 100) u, generate_series(0, 4) p", 0, NULL);
    lk_reset_counters();
}

/* The baseline you are fixing — 1 + N queries. */
static Result fetch_n_plus_one(void) {
    PGresult *users = lk_query("SELECT id FROM n1_users ORDER BY id", 0, NULL);
    int nu = PQntuples(users), posts = 0;
    for (int i = 0; i < nu; i++) {
        const char *params[] = {PQgetvalue(users, i, 0)};
        PGresult *p = lk_query("SELECT id FROM n1_posts WHERE user_id = $1", 1, params);
        posts += PQntuples(p);
        PQclear(p);
    }
    PQclear(users);
    return (Result){nu, posts};
}

static Result fetch_join(void) {
    PGresult *r = lk_query(
        "SELECT u.id AS user_id, p.id AS post_id FROM n1_users u "
        "LEFT JOIN n1_posts p ON p.user_id = u.id ORDER BY u.id, p.id",
        0, NULL);
    int users = 0, posts = 0, last = -1;
    for (int i = 0; i < PQntuples(r); i++) {
        int uid = atoi(PQgetvalue(r, i, 0));
        if (uid != last) { users++; last = uid; }
        if (!PQgetisnull(r, i, 1)) posts++;
    }
    PQclear(r);
    return (Result){users, posts};
}

/* Build a Postgres array literal "{1,2,...}" from ids. */
static void array_literal(const int *ids, int n, char *buf, int cap) {
    int off = snprintf(buf, cap, "{");
    for (int i = 0; i < n; i++) off += snprintf(buf + off, cap - off, i ? ",%d" : "%d", ids[i]);
    snprintf(buf + off, cap - off, "}");
}

static Result fetch_in_batch(void) {
    PGresult *users = lk_query("SELECT id FROM n1_users ORDER BY id", 0, NULL);
    int nu = PQntuples(users);
    int ids[256];
    for (int i = 0; i < nu; i++) ids[i] = atoi(PQgetvalue(users, i, 0));
    PQclear(users);

    char lit[2048];
    array_literal(ids, nu, lit, sizeof(lit));
    const char *params[] = {lit};
    PGresult *p = lk_query("SELECT id FROM n1_posts WHERE user_id = ANY($1::int[])", 1, params);
    int posts = PQntuples(p);
    PQclear(p);
    return (Result){nu, posts};
}

/* Part 2 — DataLoader: N threads each load(); the batch fires one query when all
 * `expected` ids have arrived (deterministic query count). */
typedef struct {
    int expected, arrived, done;
    int ids[256];
    int counts[256]; /* counts[uid] = number of posts */
    pthread_mutex_t mu;
    pthread_cond_t cond;
} Loader;

static void loader_init(Loader *l, int expected) {
    l->expected = expected;
    l->arrived = 0;
    l->done = 0;
    memset(l->counts, 0, sizeof(l->counts));
    pthread_mutex_init(&l->mu, NULL);
    pthread_cond_init(&l->cond, NULL);
}

static void dispatch(Loader *l) {
    char lit[2048];
    array_literal(l->ids, l->arrived, lit, sizeof(lit));
    const char *params[] = {lit};
    PGresult *r = lk_query("SELECT user_id FROM n1_posts WHERE user_id = ANY($1::int[])", 1, params);
    for (int i = 0; i < PQntuples(r); i++) l->counts[atoi(PQgetvalue(r, i, 0))]++;
    PQclear(r);
}

static int loader_load(Loader *l, int uid) {
    pthread_mutex_lock(&l->mu);
    l->ids[l->arrived++] = uid;
    if (l->arrived == l->expected) {
        dispatch(l);
        l->done = 1;
        pthread_cond_broadcast(&l->cond);
    } else {
        while (!l->done) pthread_cond_wait(&l->cond, &l->mu);
    }
    int c = l->counts[uid];
    pthread_mutex_unlock(&l->mu);
    return c;
}

typedef struct {
    Loader *l;
    int uid;
    int result;
} Task;

static void *dl_worker(void *arg) {
    Task *t = (Task *)arg;
    t->result = loader_load(t->l, t->uid);
    return NULL;
}

static Result fetch_with_dataloader(void) {
    PGresult *users = lk_query("SELECT id FROM n1_users ORDER BY id", 0, NULL);
    int nu = PQntuples(users);
    Loader l;
    loader_init(&l, nu);
    pthread_t threads[256];
    Task tasks[256];
    for (int i = 0; i < nu; i++) {
        tasks[i].l = &l;
        tasks[i].uid = atoi(PQgetvalue(users, i, 0));
        pthread_create(&threads[i], NULL, dl_worker, &tasks[i]);
    }
    PQclear(users);
    int posts = 0;
    for (int i = 0; i < nu; i++) {
        pthread_join(threads[i], NULL);
        posts += tasks[i].result;
    }
    return (Result){nu, posts};
}

int main(void) {
    lk_init();
    setup_dataset();
    printf("Seeded %d users x %d posts in Postgres\n", USER_COUNT, POSTS_PER_USER);

    lk_reset_counters();
    Result n1 = fetch_n_plus_one();
    assert(lk_query_count() == 101 && "N+1 should run 101 queries");

    lk_reset_counters();
    Result jn = fetch_join();
    assert(lk_query_count() == 1 && "JOIN should run 1 query");

    lk_reset_counters();
    Result bt = fetch_in_batch();
    assert(lk_query_count() == 2 && "IN batch should run 2 queries");

    lk_reset_counters();
    Result dl = fetch_with_dataloader();
    assert(lk_query_count() == 2 && "DataLoader should run 2 queries (1 users + 1 batched)");

    Result all[] = {n1, jn, bt, dl};
    for (int i = 0; i < 4; i++) {
        assert(all[i].users == 100 && "all return 100 users");
        assert(all[i].posts == 500 && "all return 500 posts");
    }

    printf("OK — N+1=101, JOIN=1, IN batch=2, DataLoader=2 queries\n");
    return 0;
}
