// Lab 02: Connection Pool Tuning — C++, YOUR TURN.
//
// Implement Pool::acquire and Pool::release so the pool reuses idle connections,
// throws PoolTimeout when exhausted, and never creates more than maxSize.
//
//   Run / validate:  g++ -std=c++20 -O2 -pthread stub.cpp -o /tmp/lab02_stub && /tmp/lab02_stub
//   Reference:        g++ -std=c++20 -O2 -pthread solution.cpp -o /tmp/lab02_cpp && /tmp/lab02_cpp
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <semaphore>
#include <stdexcept>
#include <thread>
#include <vector>

using namespace std::chrono_literals;

struct Conn {
    int id;
    void execute() const { std::this_thread::sleep_for(10ms); }
};

struct PoolTimeout : std::runtime_error {
    PoolTimeout() : std::runtime_error("pool exhausted: acquire timed out") {}
};

class Pool {
    std::chrono::milliseconds timeout_;
    std::counting_semaphore<> sem_;
    std::mutex mu_;
    std::vector<Conn> idle_;
    int created_ = 0;

public:
    Pool(int maxSize, std::chrono::milliseconds timeout)
        : timeout_(timeout), sem_(maxSize) {}

    // TODO:
    //   1. sem_.try_acquire_for(timeout_); if it returns false, throw PoolTimeout().
    //   2. Under mu_: pop an idle Conn if any (return it).
    //   3. Otherwise newId = ++created_, unlock, sleep_for(15ms), return Conn{newId}.
    Conn acquire() {
        throw std::logic_error("TODO: implement Pool::acquire");
    }

    // TODO:
    //   Under mu_: push c to idle_. Then sem_.release().
    void release(const Conn& c) {
        (void)c;
        throw std::logic_error("TODO: implement Pool::release");
    }

    int created() {
        std::lock_guard<std::mutex> lk(mu_);
        return created_;
    }
};

int main() {
    Pool p(2, 5s);
    Conn c1 = p.acquire();
    p.release(c1);
    Conn c2 = p.acquire();
    assert(c1.id == c2.id && "released connection should be reused");
    p.release(c2);

    Pool p2(1, 200ms);
    Conn held = p2.acquire();
    bool timed_out = false;
    try {
        p2.acquire();
    } catch (const PoolTimeout&) {
        timed_out = true;
    }
    assert(timed_out && "second acquire should time out");
    p2.release(held);

    Pool p3(10, 30s);
    std::atomic<int> ok{0};
    std::vector<std::thread> threads;
    for (int i = 0; i < 100; ++i) {
        threads.emplace_back([&] {
            Conn c = p3.acquire();
            c.execute();
            p3.release(c);
            ok.fetch_add(1);
        });
    }
    for (auto& t : threads) t.join();
    assert(ok.load() == 100 && "all 100 requests should succeed");
    assert(p3.created() <= 10 && "pool must never create more than maxSize connections");

    std::printf("OK — reuse, timeout, and bound (created=%d for 100 requests, max=10)\n",
                p3.created());
    return 0;
}
