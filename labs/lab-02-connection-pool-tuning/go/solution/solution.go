// Lab 02: Connection Pool Tuning — Go reference solution.
//
// A bounded, thread-safe connection pool. Creation is expensive (15ms),
// queries are cheap (10ms). The pool must never create more than maxSize
// connections, and acquire() must block (with a timeout) when exhausted.
//
//	Run:  go run solution.go
package main

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

const (
	connectionCreationCost = 15 * time.Millisecond
	queryCost              = 10 * time.Millisecond
)

var errPoolExhausted = errors.New("pool exhausted: acquire timed out")

type Conn struct{ ID int }

func (c *Conn) Execute() { time.Sleep(queryCost) }

type Pool struct {
	maxSize int
	timeout time.Duration
	sem     chan struct{} // counting semaphore: maxSize permits
	mu      sync.Mutex
	idle    []*Conn
	created int
}

func NewPool(maxSize int, timeout time.Duration) *Pool {
	return &Pool{maxSize: maxSize, timeout: timeout, sem: make(chan struct{}, maxSize)}
}

// Acquire takes a permit (blocking up to timeout), then reuses an idle
// connection or creates a new one. A permit is held until Release, so at most
// maxSize connections are ever created.
func (p *Pool) Acquire() (*Conn, error) {
	select {
	case p.sem <- struct{}{}:
	case <-time.After(p.timeout):
		return nil, errPoolExhausted
	}

	p.mu.Lock()
	if n := len(p.idle); n > 0 {
		c := p.idle[n-1]
		p.idle = p.idle[:n-1]
		p.mu.Unlock()
		return c, nil
	}
	p.created++
	id := p.created
	p.mu.Unlock()

	time.Sleep(connectionCreationCost) // create outside the lock
	return &Conn{ID: id}, nil
}

// Release returns the connection to the idle set and frees one permit.
func (p *Pool) Release(c *Conn) {
	p.mu.Lock()
	p.idle = append(p.idle, c)
	p.mu.Unlock()
	<-p.sem
}

func (p *Pool) Created() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.created
}

// ── checks (act as the test; non-zero exit on failure) ──

func assert(cond bool, msg string) {
	if !cond {
		panic("CHECK FAILED: " + msg)
	}
}

func main() {
	// 1. reuse: release then acquire returns the same connection
	p := NewPool(2, 5*time.Second)
	c1, _ := p.Acquire()
	p.Release(c1)
	c2, _ := p.Acquire()
	assert(c1.ID == c2.ID, "released connection should be reused")
	p.Release(c2)

	// 2. timeout when exhausted
	p2 := NewPool(1, 200*time.Millisecond)
	held, _ := p2.Acquire()
	_, err := p2.Acquire()
	assert(errors.Is(err, errPoolExhausted), "second acquire should time out")
	p2.Release(held)

	// 3. never exceed maxSize under concurrent load
	p3 := NewPool(10, 30*time.Second)
	var wg sync.WaitGroup
	var ok int
	var okMu sync.Mutex
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, err := p3.Acquire()
			if err != nil {
				return
			}
			c.Execute()
			p3.Release(c)
			okMu.Lock()
			ok++
			okMu.Unlock()
		}()
	}
	wg.Wait()
	assert(ok == 100, "all 100 requests should succeed")
	assert(p3.Created() <= 10, "pool must never create more than maxSize connections")

	fmt.Printf("OK — reuse, timeout, and bound (created=%d for 100 requests, max=10)\n", p3.Created())
}
