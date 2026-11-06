// Run against a protected, offline database copy; never print push credentials.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"go.etcd.io/bbolt"
)

func main() {
	database := flag.String("db", "", "path to a protected offline bbolt snapshot")
	endpoint := flag.String("endpoint", "", "internal server URL")
	commit := flag.String("commit", "", "expected server source revision")
	flag.Parse()
	if err := probe(*database, strings.TrimRight(*endpoint, "/"), *commit); err != nil {
		fmt.Fprintln(os.Stderr, "validation failed:", err)
		os.Exit(1)
	}
}

func probe(path, endpoint, commit string) error {
	db, err := bbolt.Open(path, 0400, &bbolt.Options{ReadOnly: true, Timeout: time.Second})
	if err != nil {
		return err
	}
	defer db.Close()
	var key, token string
	count := 0
	fingerprint := sha256.New()
	err = db.View(func(tx *bbolt.Tx) error {
		var checkErr error
		for err := range tx.Check() {
			checkErr = fmt.Errorf("database consistency check failed: %w", err)
		}
		if checkErr != nil {
			return checkErr
		}
		bucket := tx.Bucket([]byte("device"))
		if bucket == nil {
			return fmt.Errorf("device bucket missing")
		}
		return bucket.ForEach(func(k, v []byte) error {
			count++
			for _, part := range [][]byte{k, v} {
				_ = binary.Write(fingerprint, binary.BigEndian, uint64(len(part)))
				_, _ = fingerprint.Write(part)
			}
			if key == "" && len(v) >= 32 && !bytes.ContainsAny(v, " \n\t") {
				key, token = string(k), string(v)
			}
			return nil
		})
	})
	if err != nil {
		return err
	}
	if endpoint == "" {
		return json.NewEncoder(os.Stdout).Encode(map[string]any{"registration_count": count,
			"registration_fingerprint": hex.EncodeToString(fingerprint.Sum(nil)), "bbolt_integrity": "passed"})
	}
	if key == "" {
		return fmt.Errorf("no existing registration suitable for idempotent replay")
	}
	client := &http.Client{Timeout: 10 * time.Second}
	request := func(method, route string, data any, expected int, result any) error {
		body, err := json.Marshal(data)
		if err != nil {
			return err
		}
		req, err := http.NewRequest(method, endpoint+route, bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		res, err := client.Do(req)
		if err != nil {
			return err
		}
		defer res.Body.Close()
		if res.StatusCode != expected {
			return fmt.Errorf("%s %s: unexpected status %d", method, route, res.StatusCode)
		}
		if result != nil {
			return json.NewDecoder(res.Body).Decode(result)
		}
		return nil
	}
	if err := request("GET", "/healthz", nil, 200, nil); err != nil {
		return err
	}
	if err := request("POST", "/register", map[string]string{}, 403, nil); err != nil {
		return err
	}
	if err := request("POST", "/register", map[string]string{"key": "unknown-probe-key", "devicetoken": token}, 403, nil); err != nil {
		return err
	}
	var response struct {
		Code int               `json:"code"`
		Data map[string]string `json:"data"`
	}
	if err := request("POST", "/register", map[string]string{"key": key, "devicetoken": token}, 200, &response); err != nil {
		return err
	}
	if response.Code != 200 || response.Data["key"] != key || response.Data["device_token"] != token {
		return fmt.Errorf("registration changed key or token")
	}
	if err := request("POST", "/push", map[string]string{"body": "probe-without-key"}, 400, nil); err != nil {
		return err
	}
	var info struct {
		Commit  string `json:"commit"`
		Devices int    `json:"devices"`
	}
	if err := request("GET", "/info", nil, 200, &info); err != nil {
		return err
	}
	if info.Devices != count || info.Commit != commit {
		return fmt.Errorf("registration count or deployed revision mismatch")
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"health": "passed", "closed_enrollment": "passed", "existing_registration_replay": "passed", "missing_key_rejected": true, "registration_count": count, "commit": info.Commit, "apns_delivery": "not attempted"})
}
