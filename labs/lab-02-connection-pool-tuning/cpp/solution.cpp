// Lab 02: Connection Pool Tuning — C++ reference solution.
//
//   Run:  g++ -std=c++20 -O2 -pthread solution.cpp -o /tmp/lab02_cpp && /tmp/lab02_cpp
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

// Thread-safe bounded pool. A counting semaphore caps concurrent holders at
// maxSize, so the pool never creates more than maxSize connections.
class Pool {
    std::chrono::milliseconds timeout_;
    std::counting_semaphore<> sem_;
    std::mutex mu_;
    std::vector<Conn> idle_;
    int created_ = 0;

public:
    Pool(int maxSize, std::chrono::milliseconds timeout)
        : timeout_(timeout), sem_(maxSize) {}

    Conn acquire() {
        if (!sem_.try_acquire_for(timeout_)) throw PoolTimeout();
        int newId;
        {
            std::lock_guard<std::mutex> lk(mu_);
            if (!idle_.empty()) {
                Conn c = idle_.back();
                idle_.pop_back();
                return c;
            }
            newId = ++created_;
        }
        std::this_thread::sleep_for(15ms); // create outside the lock
        return Conn{newId};
    }

    void release(const Conn& c) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            idle_.push_back(c);
        }
        sem_.release();
    }

    int created() {
        std::lock_guard<std::mutex> lk(mu_);
        return created_;
    }
};

int main() {
    // 1. reuse
    Pool p(2, 5s);
    Conn c1 = p.acquire();
    p.release(c1);
    Conn c2 = p.acquire();
    assert(c1.id == c2.id && "released connection should be reused");
    p.release(c2);

    // 2. timeout when exhausted
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

    // 3. never exceed maxSize under concurrent load
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
