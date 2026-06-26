// Lab 01: N+1 Query Profiling — C++ reference solution (PostgreSQL).
//
// Uses the shared libpq labkit (C ABI); lk_query_count() proves 101 -> 1 -> 2.
//   Build: gcc -c ../../../tooling/c/labkit.c -I../../../tooling/c -I$(pg_config --includedir) -o /tmp/labkit.o
//          g++ -std=c++20 solution.cpp /tmp/labkit.o -I../../../tooling/c -I$(pg_config --includedir) -lpq -lpthread -o /tmp/lab01cpp && /tmp/lab01cpp
#include "labkit.h"

#include <cassert>
#include <condition_variable>
#include <cstdlib>
#include <cstdio>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

constexpr int USER_COUNT = 100;
constexpr int POSTS_PER_USER = 5;

struct Result {
    int users;
    int posts;
};

static void setup_dataset() {
    lk_exec("DROP TABLE IF EXISTS n1_posts", 0, nullptr);
    lk_exec("DROP TABLE IF EXISTS n1_users", 0, nullptr);
    lk_exec("CREATE TABLE n1_users (id INT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)", 0, nullptr);
    lk_exec("CREATE TABLE n1_posts (id INT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, user_id INT NOT NULL REFERENCES n1_users(id))", 0, nullptr);
    lk_exec("CREATE INDEX idx_n1_posts_user_id ON n1_posts(user_id)", 0, nullptr);
    lk_exec("INSERT INTO n1_users SELECT g, 'User ' || g, 'user' || g || '@example.com' FROM generate_series(1, 100) g", 0, nullptr);
    lk_exec("INSERT INTO n1_posts SELECT u*10+p, 'Post ' || p, 'body', u FROM generate_series(1, 100) u, generate_series(0, 4) p", 0, nullptr);
    lk_reset_counters();
}

static std::string array_literal(const std::vector<int>& ids) {
    std::string s = "{";
    for (size_t i = 0; i < ids.size(); i++) {
        if (i) s += ",";
        s += std::to_string(ids[i]);
    }
    return s + "}";
}

// The baseline you are fixing — 1 + N queries.
Result fetch_n_plus_one() {
    PGresult* users = lk_query("SELECT id FROM n1_users ORDER BY id", 0, nullptr);
    int nu = PQntuples(users), posts = 0;
    for (int i = 0; i < nu; i++) {
        const char* params[] = {PQgetvalue(users, i, 0)};
        PGresult* p = lk_query("SELECT id FROM n1_posts WHERE user_id = $1", 1, params);
        posts += PQntuples(p);
        PQclear(p);
    }
    PQclear(users);
    return {nu, posts};
}

Result fetch_join() {
    PGresult* r = lk_query(
        "SELECT u.id AS user_id, p.id AS post_id FROM n1_users u "
        "LEFT JOIN n1_posts p ON p.user_id = u.id ORDER BY u.id, p.id",
        0, nullptr);
    int users = 0, posts = 0, last = -1;
    for (int i = 0; i < PQntuples(r); i++) {
        int uid = std::atoi(PQgetvalue(r, i, 0));
        if (uid != last) { users++; last = uid; }
        if (!PQgetisnull(r, i, 1)) posts++;
    }
    PQclear(r);
    return {users, posts};
}

Result fetch_in_batch() {
    PGresult* users = lk_query("SELECT id FROM n1_users ORDER BY id", 0, nullptr);
    std::vector<int> ids;
    for (int i = 0; i < PQntuples(users); i++) ids.push_back(std::atoi(PQgetvalue(users, i, 0)));
    int nu = (int)ids.size();
    PQclear(users);

    std::string lit = array_literal(ids);
    const char* params[] = {lit.c_str()};
    PGresult* p = lk_query("SELECT id FROM n1_posts WHERE user_id = ANY($1::int[])", 1, params);
    int posts = PQntuples(p);
    PQclear(p);
    return {nu, posts};
}

// Part 2 — DataLoader: N threads load(); the batch fires one query once all
// `expected` ids arrive.
class PostLoader {
public:
    explicit PostLoader(int expected) : expected_(expected) {}

    int load(int uid) {
        std::unique_lock<std::mutex> lk(mu_);
        ids_.push_back(uid);
        if ((int)ids_.size() == expected_) {
            dispatch();
            done_ = true;
            cond_.notify_all();
        } else {
            cond_.wait(lk, [this] { return done_; });
        }
        return counts_[uid];
    }

private:
    void dispatch() {
        std::string lit = array_literal(ids_);
        const char* params[] = {lit.c_str()};
        PGresult* r = lk_query("SELECT user_id FROM n1_posts WHERE user_id = ANY($1::int[])", 1, params);
        for (int i = 0; i < PQntuples(r); i++) counts_[std::atoi(PQgetvalue(r, i, 0))]++;
        PQclear(r);
    }

    int expected_;
    bool done_ = false;
    std::vector<int> ids_;
    std::map<int, int> counts_;
    std::mutex mu_;
    std::condition_variable cond_;
};

Result fetch_with_dataloader() {
    PGresult* users = lk_query("SELECT id FROM n1_users ORDER BY id", 0, nullptr);
    int nu = PQntuples(users);
    PostLoader loader(nu);
    std::vector<int> uids;
    for (int i = 0; i < nu; i++) uids.push_back(std::atoi(PQgetvalue(users, i, 0)));
    PQclear(users);

    std::vector<int> results(nu);
    std::vector<std::thread> threads;
    for (int i = 0; i < nu; i++) {
        threads.emplace_back([&loader, &results, &uids, i] { results[i] = loader.load(uids[i]); });
    }
    for (auto& t : threads) t.join();
    int posts = 0;
    for (int c : results) posts += c;
    return {nu, posts};
}

int main() {
    lk_init();
    setup_dataset();
    std::printf("Seeded %d users x %d posts in Postgres\n", USER_COUNT, POSTS_PER_USER);

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

    for (auto r : {n1, jn, bt, dl}) {
        assert(r.users == 100 && "all return 100 users");
        assert(r.posts == 500 && "all return 500 posts");
    }

    std::printf("OK — N+1=101, JOIN=1, IN batch=2, DataLoader=2 queries\n");
    return 0;
}
