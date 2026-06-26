/* Lab 02: Connection Pool Tuning — C reference solution.
 *
 *   Run:  gcc -O2 -pthread solution.c -o /tmp/lab02_c && /tmp/lab02_c
 *
 * A bounded, thread-safe pool. A POSIX semaphore caps concurrent holders at
 * max_size, so the pool never creates more than max_size connections.
 * A connection is just its integer id here.
 */
#include <assert.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdio.h>
#include <time.h>

#define CREATE_NS 15000000L /* 15 ms */
#define QUERY_NS 10000000L  /* 10 ms */
#define IDLE_CAP 1024

typedef struct {
    sem_t sem;
    pthread_mutex_t mu;
    int idle_ids[IDLE_CAP];
    int idle_count;
    int created;
} Pool;

static void sleep_ns(long ns) {
    struct timespec ts = {.tv_sec = 0, .tv_nsec = ns};
    nanosleep(&ts, NULL);
}

static void conn_execute(void) { sleep_ns(QUERY_NS); }

void pool_init(Pool *p, int max_size) {
    sem_init(&p->sem, 0, (unsigned)max_size);
    pthread_mutex_init(&p->mu, NULL);
    p->idle_count = 0;
    p->created = 0;
}

/* Returns a connection id, or -1 if the acquire timed out. */
int pool_acquire(Pool *p, long timeout_ms) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += timeout_ms / 1000;
    ts.tv_nsec += (timeout_ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L) {
        ts.tv_sec += 1;
        ts.tv_nsec -= 1000000000L;
    }
    if (sem_timedwait(&p->sem, &ts) != 0) return -1; /* exhausted */

    int id;
    pthread_mutex_lock(&p->mu);
    if (p->idle_count > 0) {
        id = p->idle_ids[--p->idle_count];
        pthread_mutex_unlock(&p->mu);
        return id;
    }
    id = ++p->created;
    pthread_mutex_unlock(&p->mu);

    sleep_ns(CREATE_NS); /* create outside the lock */
    return id;
}

void pool_release(Pool *p, int id) {
    pthread_mutex_lock(&p->mu);
    p->idle_ids[p->idle_count++] = id;
    pthread_mutex_unlock(&p->mu);
    sem_post(&p->sem);
}

/* ── concurrent workload ── */

static Pool *g_pool;
static pthread_mutex_t g_ok_mu = PTHREAD_MUTEX_INITIALIZER;
static int g_ok;

static void *worker(void *arg) {
    (void)arg;
    int id = pool_acquire(g_pool, 30000);
    if (id < 0) return NULL;
    conn_execute();
    pool_release(g_pool, id);
    pthread_mutex_lock(&g_ok_mu);
    g_ok++;
    pthread_mutex_unlock(&g_ok_mu);
    return NULL;
}

int main(void) {
    /* 1. reuse */
    Pool p1;
    pool_init(&p1, 2);
    int c1 = pool_acquire(&p1, 5000);
    pool_release(&p1, c1);
    int c2 = pool_acquire(&p1, 5000);
    assert(c1 == c2 && "released connection should be reused");
    pool_release(&p1, c2);

    /* 2. timeout when exhausted */
    Pool p2;
    pool_init(&p2, 1);
    int held = pool_acquire(&p2, 5000);
    int second = pool_acquire(&p2, 200);
    assert(second == -1 && "second acquire should time out");
    pool_release(&p2, held);

    /* 3. never exceed max_size under concurrent load */
    Pool p3;
    pool_init(&p3, 10);
    g_pool = &p3;
    g_ok = 0;
    pthread_t threads[100];
    for (int i = 0; i < 100; i++) pthread_create(&threads[i], NULL, worker, NULL);
    for (int i = 0; i < 100; i++) pthread_join(threads[i], NULL);
    assert(g_ok == 100 && "all 100 requests should succeed");
    assert(p3.created <= 10 && "pool must never create more than max_size connections");

    printf("OK — reuse, timeout, and bound (created=%d for 100 requests, max=10)\n", p3.created);
    return 0;
}
