package database

import (
	"sync"
	"testing"
)

func TestBboltRegistrationSurvivesRestartAndTokenRotation(t *testing.T) {
	directory := t.TempDir()
	first := NewBboltdb(directory)
	key, err := first.SaveDeviceTokenByKey("", "original-token")
	if err != nil || key == "" {
		t.Fatalf("new registration: %v", err)
	}
	updated, err := first.SaveDeviceTokenByKey(key, "rotated-token")
	if err != nil || updated != key {
		t.Fatal("rotation changed the push key")
	}
	// Older runtimes invalidated tokens without deleting their bearer keys.
	if _, err := first.SaveDeviceTokenByKey(key, ""); err != nil {
		t.Fatal(err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	// Simulate a fresh process while keeping exactly the same data directory.
	dbOnce = sync.Once{}
	second := NewBboltdb(directory)
	defer second.Close()
	if exists, err := second.HasDeviceKey(key); err != nil || !exists {
		t.Fatal("empty-token registration lost on restart")
	}
	if exists, err := second.HasDeviceKey("unknown"); err != nil || exists {
		t.Fatal("unknown key accepted")
	}
	if _, err := second.DeviceTokenByKey(key); err == nil {
		t.Fatal("empty token treated as deliverable")
	}
	if updated, err := second.SaveDeviceTokenByKey(key, "rotated-token"); err != nil || updated != key {
		t.Fatal("empty-token recovery changed key")
	}
	if token, err := second.DeviceTokenByKey(key); err != nil || token != "rotated-token" {
		t.Fatalf("registration lost on restart: %v", err)
	}
	if count, err := second.CountAll(); err != nil || count != 1 {
		t.Fatalf("expected one registration, got %d: %v", count, err)
	}
}
