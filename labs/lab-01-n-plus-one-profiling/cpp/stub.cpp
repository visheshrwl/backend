// Lab 01: N+1 Query Profiling — C++, YOUR TURN (PostgreSQL).
//
// lk_query_count() is your proof. Baseline and setup are given. Implement:
//   Part 1: fetch_join (1 query), fetch_in_batch (2 queries, = ANY($1::int[]))
//   Part 2: PostLoader::load / dispatch — N load() calls -> ONE query
//
//   Build the stub: gcc -c ../../../tooling/c/labkit.c -I../../../tooling/c -I$(pg_config --includedir) -o /tmp/labkit.o
//                   g++ -std=c++20 stub.cpp /tmp/labkit.o -I../../../tooling/c -I$(pg_config --includedir) -lpq -lpthread -o /tmp/lab01cpp && /tmp/lab01cpp
#include "labkit.h"

#include <cassert>
#include <condition_variable>
#include <cstdlib>
#include <cstdio>
#include <map>
#include <mutex>
#include <stdexcept>
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

// The baseline you are fixing. (given)
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

// TODO: same data in EXACTLY ONE query (LEFT JOIN). Count distinct users and
// non-null posts (PQgetisnull). Return {users, posts}.
Result fetch_join() {
    throw std::logic_error("TODO: implement fetch_join");
}

// TODO: EXACTLY TWO queries; query 2 uses WHERE user_id = ANY($1::int[]) with
// array_literal(ids). Return {users, posts}.
Result fetch_in_batch() {
    throw std::logic_error("TODO: implement fetch_in_batch");
}

// Part 2 — DataLoader.
class PostLoader {
public:
    explicit PostLoader(int expected) : expected_(expected) {}

    // TODO: under mu_, push uid to ids_. When ids_.size() == expected_, call
    // dispatch(), set done_, notify_all. Otherwise cond_.wait until done_.
    // Return counts_[uid].
    int load(int uid) {
        (void)uid;
        throw std::logic_error("TODO: implement PostLoader::load");
    }

private:
    // TODO: run ONE query SELECT user_id FROM n1_posts WHERE user_id = ANY($1::int[])
    // for ids_, and increment counts_[user_id] per row.
    void dispatch() {
        throw std::logic_error("TODO: implement PostLoader::dispatch");
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
