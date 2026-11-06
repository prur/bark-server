package database

// Database defines all of the db operation
type Database interface {
	CountAll() (int, error)                                 //Get db records count
	HasDeviceKey(key string) (bool, error)                  //Registration existence, including invalidated/empty tokens
	DeviceTokenByKey(key string) (string, error)            //Get specified device's token
	SaveDeviceTokenByKey(key, token string) (string, error) //Create or update specified devices's token
	DeleteDeviceByKey(key string) error                     //Delete specified device
	Close() error                                           //Close the database
}
