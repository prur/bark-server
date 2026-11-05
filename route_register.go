package main

import (
	"fmt"
	"github.com/gofiber/fiber/v2"
	"github.com/mritd/logger"
	"strings"
)

var registrationMode = "open"

func setRegistrationMode(mode string) error {
	mode = strings.ToLower(strings.TrimSpace(mode))
	switch mode {
	case "open", "existing", "disabled":
		registrationMode = mode
		return nil
	default:
		return fmt.Errorf("registration mode must be open, existing, or disabled")
	}
}

type DeviceInfo struct {
	DeviceKey   string `form:"device_key,omitempty" json:"device_key,omitempty" xml:"device_key,omitempty" query:"device_key,omitempty"`
	DeviceToken string `form:"device_token,omitempty" json:"device_token,omitempty" xml:"device_token,omitempty" query:"device_token,omitempty"`

	// compatible with old req
	OldDeviceKey   string `form:"key,omitempty" json:"key,omitempty" xml:"key,omitempty" query:"key,omitempty"`
	OldDeviceToken string `form:"devicetoken,omitempty" json:"devicetoken,omitempty" xml:"devicetoken,omitempty" query:"devicetoken,omitempty"`
}

func init() {
	registerRoute("register", func(router fiber.Router) {
		router.Post("/register", func(c *fiber.Ctx) error { return doRegister(c, false) })
		router.Get("/register/:device_key", doRegisterCheck)
	})

	// compatible with old requests
	registerRouteWithWeight("register_compat", 100, func(router fiber.Router) {
		router.Get("/register", func(c *fiber.Ctx) error { return doRegister(c, true) })
	})
}

func doRegister(c *fiber.Ctx, compat bool) error {
	if registrationMode == "disabled" {
		return c.Status(403).JSON(failed(403, "registration is disabled"))
	}
	var deviceInfo DeviceInfo
	if compat {
		if err := c.QueryParser(&deviceInfo); err != nil {
			return c.Status(400).JSON(failed(400, "request bind failed1: %v", err))
		}
	} else {
		if err := c.BodyParser(&deviceInfo); err != nil {
			return c.Status(400).JSON(failed(400, "request bind failed2: %v", err))
		}
	}

	if deviceInfo.DeviceKey == "" && deviceInfo.OldDeviceKey != "" {
		deviceInfo.DeviceKey = deviceInfo.OldDeviceKey
	}
	// A push key is the existing client's bearer credential. Closed enrollment
	// must still let that client refresh a rotated APNs token. Never silently
	// generate a replacement key if its registration is missing.
	if registrationMode == "existing" {
		if deviceInfo.DeviceKey == "" {
			return c.Status(403).JSON(failed(403, "new device registration is disabled"))
		}
		if _, err := db.DeviceTokenByKey(deviceInfo.DeviceKey); err != nil {
			return c.Status(403).JSON(failed(403, "new device registration is disabled"))
		}
	}

	if deviceInfo.DeviceToken == "" {
		if deviceInfo.OldDeviceToken != "" {
			deviceInfo.DeviceToken = deviceInfo.OldDeviceToken
		} else {
			return c.Status(400).JSON(failed(400, "device token is empty"))
		}
	}

	// DeviceToken length is variable, but should not be too long.
	if len(deviceInfo.DeviceToken) > 160 {
		return c.Status(400).JSON(failed(400, "device token is invalid"))
	}

	// if deviceInfo.DeviceKey=="", newKey will be filled with a new uuid
	// otherwise it equal to deviceInfo.DeviceKey
	newKey, err := db.SaveDeviceTokenByKey(deviceInfo.DeviceKey, deviceInfo.DeviceToken)
	if err != nil {
		logger.Errorf("device registration failed: %v", err)
		return c.Status(500).JSON(failed(500, "device registration failed: %v", err))
	}
	deviceInfo.DeviceKey = newKey

	return c.Status(200).JSON(data(map[string]string{
		// compatible with old resp
		"key":          deviceInfo.DeviceKey,
		"device_key":   deviceInfo.DeviceKey,
		"device_token": deviceInfo.DeviceToken,
	}))
}

func doRegisterCheck(c *fiber.Ctx) error {
	deviceKey := c.Params("device_key")

	if deviceKey == "" {
		return c.Status(400).JSON(failed(400, "device key is empty"))
	}

	_, err := db.DeviceTokenByKey(deviceKey)
	if err != nil {
		return c.Status(400).JSON(failed(400, "%s", err.Error()))
	}
	return c.Status(200).JSON(success())
}
