package main

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"testing"

	"go.etcd.io/bbolt"
)

func TestOfflineProbeChecksRealDatabaseWithoutPrintingOrChangingCredentials(t *testing.T) {
	path := filepath.Join(t.TempDir(), "copy.db")
	db, err := bbolt.Open(path, 0600, nil)
	if err != nil {
		t.Fatal(err)
	}
	err = db.Update(func(tx *bbolt.Tx) error {
		bucket, err := tx.CreateBucket([]byte("device"))
		if err != nil {
			return err
		}
		if err := bucket.Put([]byte("fixture-private-key"), bytes.Repeat([]byte("a"), 64)); err != nil {
			return err
		}
		return bucket.Put([]byte("fixture-empty-token-key"), []byte{})
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(path)
	original := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	defer func() { os.Stdout = original }()
	probeErr := probe(path, "", "")
	w.Close()
	output, _ := io.ReadAll(r)
	r.Close()
	if probeErr != nil {
		t.Fatal(probeErr)
	}
	var result struct {
		Count       int    `json:"registration_count"`
		Fingerprint string `json:"registration_fingerprint"`
		Integrity   string `json:"bbolt_integrity"`
	}
	if err := json.Unmarshal(output, &result); err != nil {
		t.Fatal(err)
	}
	if result.Count != 2 || len(result.Fingerprint) != 64 || result.Integrity != "passed" {
		t.Fatalf("unexpected inventory: %s", output)
	}
	if bytes.Contains(output, []byte("fixture-private")) || bytes.Contains(output, bytes.Repeat([]byte("a"), 64)) {
		t.Fatal("probe exposed credentials")
	}
	after, _ := os.ReadFile(path)
	if !bytes.Equal(before, after) {
		t.Fatal("offline probe changed the database")
	}
}
