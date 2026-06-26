// Lab 03: Cache-Aside with Postgres + Redis — C++ reference solution.
//
// Uses the shared libpq + RESP labkit (C ABI) with idiomatic C++.
//   Build: gcc -c ../../../tooling/c/labkit.c -I../../../tooling/c -I$(pg_config --includedir) -o /tmp/labkit.o
//          g++ -std=c++20 solution.cpp /tmp/labkit.o -I../../../tooling/c -I$(pg_config --includedir) -lpq -lpthread -o /tmp/lab03cpp && /tmp/lab03cpp
#include "labkit.h"

#include <cassert>
#include <cstdlib>
#include <cstdio>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

constexpr int TTL = 60;

struct User {
    int id;
    std::string name, email, plan;
};

static std::string user_key(int id) { return "user:" + std::to_string(id); }

static std::string serialize(const User& u) {
    return std::to_string(u.id) + "|" + u.name + "|" + u.email + "|" + u.plan;
}

static User deserialize(const std::string& s) {
    std::vector<std::string> parts;
    size_t start = 0, pos;
    while ((pos = s.find('|', start)) != std::string::npos) {
        parts.push_back(s.substr(start, pos - start));
        start = pos + 1;
    }
    parts.push_back(s.substr(start));
    return User{std::stoi(parts[0]), parts[1], parts[2], parts[3]};
}

// Part 1 — cache-aside read.
std::optional<User> get_user_profile(int id) {
    std::string key = user_key(id);
    if (char* c = lk_cache_get(key.c_str())) {
        std::string s(c);
        free(c);
        return deserialize(s);
    }
    std::string idbuf = std::to_string(id);
    const char* params[] = {idbuf.c_str()};
    PGresult* res = lk_query("SELECT id, name, email, plan FROM users WHERE id = $1", 1, params);
    if (PQntuples(res) == 0) {
        PQclear(res);
        return std::nullopt;
    }
    User u{std::atoi(PQgetvalue(res, 0, 0)), PQgetvalue(res, 0, 1), PQgetvalue(res, 0, 2), PQgetvalue(res, 0, 3)};
    PQclear(res);
    lk_cache_set(key.c_str(), serialize(u).c_str(), TTL);
    return u;
}

void update_user_plan(int id, const std::string& plan) {
    std::string idbuf = std::to_string(id);
    const char* params[] = {plan.c_str(), idbuf.c_str()};
    lk_exec("UPDATE users SET plan = $1, updated_at = now() WHERE id = $2", 2, params);
    lk_cache_del(user_key(id).c_str());
}

// Part 2 — single-flight (one std::mutex per key + double-check).
static std::mutex g_locks_guard;
static std::map<std::string, std::unique_ptr<std::mutex>> g_locks;

static std::mutex& key_lock(const std::string& k) {
    std::lock_guard<std::mutex> g(g_locks_guard);
    auto& m = g_locks[k];
    if (!m) m = std::make_unique<std::mutex>();
    return *m;
}

std::optional<User> get_user_profile_singleflight(int id) {
    std::string key = user_key(id);
    if (char* c = lk_cache_get(key.c_str())) {
        std::string s(c);
        free(c);
        return deserialize(s);
    }
    std::lock_guard<std::mutex> g(key_lock(key));
    if (char* c = lk_cache_get(key.c_str())) { // double-check
        std::string s(c);
        free(c);
        return deserialize(s);
    }
    return get_user_profile(id); // cold path: queries once, populates
}

int main() {
    lk_init();
    lk_exec("UPDATE users SET plan = 'pro' WHERE id = 1", 0, nullptr);
    lk_cache_flush();
    lk_reset_counters();

    auto u = get_user_profile(1);
    assert(u && u->id == 1 && "user 1 should be fetched");
    assert(lk_query_count() >= 1 && "first read should hit Postgres");
    assert(lk_cache_exists("user:1") && "first read should populate the cache");

    lk_reset_counters();
    auto u2 = get_user_profile(1);
    assert(u2 && u2->id == 1 && "second read returns the user");
    assert(lk_query_count() == 0 && "a cache hit must not query Postgres");

    update_user_plan(1, "enterprise");
    assert(!lk_cache_exists("user:1") && "update must invalidate the cache");
    assert(get_user_profile(1)->plan == "enterprise" && "refilled with the new plan");

    assert(!get_user_profile(999999) && "missing user returns nullopt");

    lk_cache_flush();
    lk_reset_counters();
    std::vector<std::thread> threads;
    for (int i = 0; i < 50; i++) threads.emplace_back([] { get_user_profile_singleflight(1); });
    for (auto& t : threads) t.join();
    assert(lk_query_count() == 1 && "single-flight: only one DB query for 50 concurrent reads");

    std::printf("OK — cache-aside, invalidation, and single-flight (1 DB query for 50 concurrent reads)\n");
    return 0;
}
