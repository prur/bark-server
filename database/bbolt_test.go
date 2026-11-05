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
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	// Simulate a fresh process while keeping exactly the same data directory.
	dbOnce = sync.Once{}
	second := NewBboltdb(directory)
	defer second.Close()
	if token, err := second.DeviceTokenByKey(key); err != nil || token != "rotated-token" {
		t.Fatalf("registration lost on restart: %v", err)
	}
	if count, err := second.CountAll(); err != nil || count != 1 {
		t.Fatalf("expected one registration, got %d: %v", count, err)
	}
}
