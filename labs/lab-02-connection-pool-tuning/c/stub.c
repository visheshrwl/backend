/* Lab 02: Connection Pool Tuning — C, YOUR TURN.
 *
 * Implement pool_acquire and pool_release so the pool reuses idle connections,
 * times out when exhausted (return -1), and never creates more than max_size.
 *
 *   Run / validate:  gcc -O2 -pthread stub.c -o /tmp/lab02_stub && /tmp/lab02_stub
 *   Reference:        gcc -O2 -pthread solution.c -o /tmp/lab02_c && /tmp/lab02_c
 */
#include <assert.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdio.h>
#include <time.h>

#define CREATE_NS 15000000L
#define QUERY_NS 10000000L
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

/* TODO:
 *   1. Build an absolute deadline (CLOCK_REALTIME + timeout_ms) and call
 *      sem_timedwait(&p->sem, &deadline); return -1 if it fails (timeout).
 *   2. Lock p->mu. If p->idle_count > 0, pop and return an idle id.
 *   3. Otherwise id = ++p->created, unlock, sleep_ns(CREATE_NS), return id.
 */
int pool_acquire(Pool *p, long timeout_ms) {
    (void)p;
    (void)timeout_ms;
    assert(0 && "TODO: implement pool_acquire");
    return -1;
}

/* TODO:
 *   Lock p->mu, push id onto p->idle_ids, unlock, then sem_post(&p->sem).
 */
void pool_release(Pool *p, int id) {
    (void)p;
    (void)id;
    assert(0 && "TODO: implement pool_release");
}

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
    Pool p1;
    pool_init(&p1, 2);
    int c1 = pool_acquire(&p1, 5000);
    pool_release(&p1, c1);
    int c2 = pool_acquire(&p1, 5000);
    assert(c1 == c2 && "released connection should be reused");
    pool_release(&p1, c2);

    Pool p2;
    pool_init(&p2, 1);
    int held = pool_acquire(&p2, 5000);
    int second = pool_acquire(&p2, 200);
    assert(second == -1 && "second acquire should time out");
    pool_release(&p2, held);

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
