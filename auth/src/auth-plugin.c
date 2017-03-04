/// @file auth-plugin.c
/// @brief AutoHome Mosquitto Authorization Plugin
/// 
/// Part of AutoHome.
/// 
/// Simple SQLite-based authorization system. Holds _username:salt:hash(salt, password)_ triplets
/// in a database, and each user has access to the corresponding topics __username/\#__.
/// One superuser may use any topic it wants.
//
// Copyright (c) 2017, Diego Guerrero
// All rights reserved.
// 
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are met:
//     * Redistributions of source code must retain the above copyright
//       notice, this list of conditions and the following disclaimer.
//     * Redistributions in binary form must reproduce the above copyright
//       notice, this list of conditions and the following disclaimer in the
//       documentation and/or other materials provided with the distribution.
//     * The names of its contributors may not be used to endorse or promote products
//       derived from this software without specific prior written permission.
// 
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
// ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
// WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
// DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS BE LIABLE FOR ANY
// DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
// (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
// LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
// ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
// SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <time.h>
#include <stdarg.h>
#include <stdio.h>

#include <sqlite3.h>
#include <mosquitto.h>
#include <mosquitto_plugin.h>
#include <sha2.h>

/// @brief Plugin-specific API return codes
enum return_codes
{
	SUCCESS              = 0,
	FAILED_SQLITE        = 1,
	NO_DB_FILE_SPECIFIED = 2,
	DB_FILE_CANTOPEN     = 3,
	DB_FILE_CANTCLOSE    = 4,
	DB_ERROR             = 5,
	NOTREQUIRED          = 102
};

/// @brief Plugin global context
///
/// Maintains information and references throughout the life of the plugin.
typedef struct Context {
	/// @brief Database connection
	sqlite3* db;
	
	/// @brief Prepared statement for password queries
	sqlite3_stmt* passquery;
	
	/// @brief Username of the superuser
	/// 
	/// This user has read and write access to any topic.
	char* superuser;
	
	/// @brief Guest secret key
	/// 
	/// Password that a guest must present to validate its access to the network.
	/// This is both a convenience feature (to stop the neighbour's devices from
	/// showing up in the pairing list) and a mild security feature (to stop DoS
	/// attacks where the client asks for usernames in order to block a device
	/// from being connected). The security feature is only meant to deter simple
	/// attacks; more complex situations should be dealt using an appropriate firewall.
	char* guestsecret;
} Context;

/// @brief Releases memory used by a context
/// @remarks SQLite objects are not considered owned by the context,
///          so they will not be released by this function
void free_context(Context* context)
{
	free(context->superuser);
	free(context->guestsecret);
	free(context);
}

/// @brief Auth plugin API version
///
/// Mosquitto checks whether the plugin uses a supported version of
/// the authorization plugin API; if not, the program will terminate on startup.
int mosquitto_auth_plugin_version(void)
{
	return MOSQ_AUTH_PLUGIN_VERSION;
}

/// @brief Prepare, evaluate and destroy an SQL statement with no outputs.
/// 
/// Prepare a statement based on the given query, evaluate it and destroy the statement.
/// Any output from the query will be disregarded. Furthermore, the statement can be
/// interrupted without stepping into its results.
/// 
/// @remarks Do not manually create a query string from data provided by the user.
///          Use an appropriate binding instead.
/// 
/// @param[in] db Database handle.
/// @param[in] query SQL query to be executed.
/// @return SQL return code. SQLITE_OK, if everything executed correctly; an SQLite error code otherwise.
static int sql_exec_void(sqlite3* db, const char* query)
{
	sqlite3_stmt* statement;
	int retval;
	
	if ((retval = sqlite3_prepare_v2(db, query, -1, &statement, NULL)) != SQLITE_OK) {
		sqlite3_finalize(statement);
		return retval;
	}
	
	retval = sqlite3_step(statement);
	
	if (retval != SQLITE_DONE && retval != SQLITE_ROW) {
		sqlite3_finalize(statement);
		return retval;
	}
	
	if ((retval = sqlite3_finalize(statement)) != SQLITE_OK) {
		return retval;
	}
	
	return SQLITE_OK;
}

/// @brief Prepare, evaluate and destroy an SQL statement with a single integer output
/// 
/// Prepare a statement based on the given query, evaluate it,
/// extract the first result and destroy the statement.
/// The query must return at least one result and its type must be integer.
/// Any additional output from the query will be disregarded. Furthermore, the statement can be
/// interrupted without stepping into the rest of the results.
/// 
/// @remarks Do not manually create a query string from data provided by the user.
///          Use an appropriate binding instead.
/// 
/// @param[in] db Database handle.
/// @param[in] query SQL query to be executed.
/// @param[out] result Queried result.
/// @return SQL return code. SQLITE_OK, if everything executed correctly; an SQLite error code otherwise.
static int sql_exec_single_int(sqlite3* db, const char* query, int* result)
{
	sqlite3_stmt* statement;
	int retval;
	
	if ((retval = sqlite3_prepare_v2(db, query, -1, &statement, NULL)) != SQLITE_OK) {
		sqlite3_finalize(statement);
		return retval;
	}
	
	if ((retval = sqlite3_step(statement)) != SQLITE_ROW) {
		sqlite3_finalize(statement);
		return retval;
	}
	
	*result = sqlite3_column_int(statement, 0);
	
	if ((retval = sqlite3_finalize(statement)) != SQLITE_OK) {
		return retval;
	}
	
	return SQLITE_OK;
}

/// @brief Create a table in the database if not already there
/// 
/// If a table does not exist, create it with the specified value definitions and constraints.
///
/// @remarks This function uses simple string interpolation so must not exposed publicly,
///          since it would allow the user to run arbitrary SQL code.
/// 
/// @param[in] db Database handle.
/// @param[in] name Table name.
/// @param[in] definition Table definition. Comma-separated enumeration of the table columns,
///                       along with their types and constraints.
/// @return Return code. If the table already exists, NOTREQUIRED; if it was succesfully created SUCCESS;
///         if an error ocurred, an appropriate SQL return code.
static int create_table(sqlite3* db, char* name, char* definition)
{
	int   count;
	int   retval;
	int   bufsize = snprintf(NULL, 0, "select count(*) from sqlite_master where type='table' and name='%s';", name) + 1;
	char* buffer  = (char*) malloc(bufsize * sizeof (char));
	
	snprintf(buffer, bufsize, "select count(*) from sqlite_master where type='table' and name='%s';", name);
	
	if ((retval = sql_exec_single_int(db, buffer, &count)) != SQLITE_OK) {
		free(buffer);
		return retval;
	}
	
	free(buffer);
	
	// note that "if not exists" ensures no errors will be raised if the table already exists
	// so the previous count(*) may seem irrelevant. It is not: "if not exists" is required to
	// be free of race conditions (otherwise this function could return an error if another process
	// created the table between the commands) and the count is required to know if there was
	// a table before (to return an appropriate code)
	bufsize = snprintf(NULL, 0, "create table if not exists %s (%s);", name, definition) + 1;
	buffer  = (char*) malloc(bufsize * sizeof (char));
	
	snprintf(buffer, bufsize, "create table if not exists %s (%s);", name, definition);
	
	if (count == 0) {
		if ((retval = sql_exec_void(db, buffer)) != SQLITE_OK) {
			free(buffer);
			return retval;
		}
		
		free(buffer);
		return SUCCESS;
	}
	
	free(buffer);
	return NOTREQUIRED;
}

/// @brief Retrieve the corresponding password hash for a given user
/// 
/// Search the database for the given username and retrieve the corresponding password hash
/// and salt. If the username is not registered, retrieve the empty string as password.
/// 
/// @param[in] pssquery Prepared statement for password extraction.
/// @param[in] username Queried username.
/// @param[out] hash Retrieved password hash.
/// @param[in] hashlen Hash buffer size (including null terminator). It must be greater than zero.
/// @param[out] salt Retrieved salt used to compute the hash.
/// @param[in] saltlen Salt buffer size (including null terminator). It must be greater than zero.
/// @return SQL return code. SQLITE_OK, if everything executed correctly; an SQLite error code otherwise.
static int retrieve_password(sqlite3_stmt* passquery, const char* username, char* hash, size_t hashlen, char* salt, size_t saltlen)
{
	int retval;
	
	if ((retval = sqlite3_reset(passquery)) != SQLITE_OK) {
		return retval;
	}
	
	if ((retval = sqlite3_bind_text(passquery, 1, username, -1, SQLITE_TRANSIENT)) != SQLITE_OK) {
		return retval;
	}
	
	retval = sqlite3_step(passquery);
	
	if (retval == SQLITE_DONE) {  // unrecognized user
		hash[0] = 0;
		salt[0] = 0;
	}
	else if (retval == SQLITE_ROW) {  // recognized user
		const unsigned char* result = sqlite3_column_text(passquery, 0);
		
		strncpy(hash, (const char*) result, hashlen);
		hash[hashlen - 1] = 0;
		
		result = sqlite3_column_text(passquery, 1);
		
		strncpy(salt, (const char*) result, saltlen);
		salt[saltlen - 1] = 0;
	}
	else {
		return retval;
	}
	
	if ((retval = sqlite3_reset(passquery)) != SQLITE_OK) {
		return retval;
	}
	
	return SQLITE_OK;
}

/// @brief Plugin initialization routine
/// 
/// Open a connection to the SQLite database.
/// 
/// @param[out] user_data Initialized plugin context, available on subsequent calls to the API.
/// @param[in] auth_opts Configuration options. Used to read the database file and the superuser name
///                      (through the auth_opt_db_file and auth_opt_superuser variables in the
///                      configuration file).
/// @param[in] auth_opt_count Number of configuration options.
/// @return Return code. On success, zero; otherwise, a number greater than zero.
int mosquitto_auth_plugin_init(void **user_data, struct mosquitto_auth_opt *auth_opts, int auth_opt_count)
{
	char*    dbfile  = NULL;
	Context* context = (Context*) calloc(1, sizeof (Context));
	*user_data       = context;
	
	for (int i = 0; i < auth_opt_count; i++) {
		if (strcmp(auth_opts[i].key, "db_file") == 0) {
			dbfile = auth_opts[i].value;
		}
		else if (strcmp(auth_opts[i].key, "superuser") == 0) {
			int sulen          = strlen(auth_opts[i].value);
			context->superuser = (char*) malloc((sulen + 1) * sizeof (char));
			strncpy(context->superuser, auth_opts[i].value, sulen);
			context->superuser[sulen] = 0;
		}
		else if (strcmp(auth_opts[i].key, "guest_secret") == 0) {
			int passlen          = strlen(auth_opts[i].value);
			context->guestsecret = (char*) malloc((passlen + 1) * sizeof (char));
			strncpy(context->guestsecret, auth_opts[i].value, passlen);
			context->guestsecret[passlen] = 0;
		}
	}
	
	if (sqlite3_initialize() != SQLITE_OK) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to initialize SQLite3.");
		
		free_context(context);
		return FAILED_SQLITE;
	}
	
	if (dbfile == NULL) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "No SQLite database specified. Check your "
		                                   "Mosquitto configuration file; it should "
		                                   "include an appropriate auth_opt_db_file variable.");
		
		free_context(context);
		return NO_DB_FILE_SPECIFIED;
	}
	
	if (sqlite3_open(dbfile, &context->db) != SQLITE_OK) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to open SQLite database.");
		
		sqlite3_close(context->db);
		free_context(context);
		return DB_FILE_CANTOPEN;
	}
	
	if (sql_exec_void(context->db, "pragma foreign_keys = on;") != SQLITE_OK) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to enable foreign keys.");
		
		sqlite3_close(context->db);
		free_context(context);
		return DB_ERROR;
	}
	
	int profretvalue = create_table(context->db, "profile", "username text not null primary key,"
	                                                        "displayname text not null unique,"
	                                                        "type text not null,"
	                                                        "connected text not null,"
	                                                        "status text not null");
	
	int authretvalue = create_table(context->db, "auth", "username text not null primary key references profile on delete cascade,"
	                                                     "hash text not null,"
	                                                     "salt text not null");
	
	int schedretvalue = create_table(context->db, "schedule", "id integer not null primary key,"
	                                                          "username text not null references profile on delete cascade,"
	                                                          "command text not null,"
	                                                          "fuzzy int not null,"
	                                                          "recurrent int not null,"
	                                                          "firedate int not null,"
	                                                          "weekday int not null,"
	                                                          "hours int not null,"
	                                                          "minutes int not null");
	
	bool error = (profretvalue  != SUCCESS && profretvalue  != NOTREQUIRED);
	error     |= (authretvalue  != SUCCESS && authretvalue  != NOTREQUIRED);
	error     |= (schedretvalue != SUCCESS && schedretvalue != NOTREQUIRED);
	
	if (!error) {
		if (profretvalue == SUCCESS && authretvalue == SUCCESS && schedretvalue == SUCCESS) {
			mosquitto_log_printf(MOSQ_LOG_NOTICE, "Uninitialized database. Creating from scratch.");
		}
		else if (profretvalue == NOTREQUIRED && authretvalue == NOTREQUIRED && schedretvalue == NOTREQUIRED) {
			// database was already OK, nothing to log
		}
		else {
			mosquitto_log_printf(MOSQ_LOG_NOTICE, "Incomplete database. Patching (but foreign keys may be wrong).");
		}
	}
	else {  // SQL error
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to create tables.");
		
		sqlite3_close(context->db);
		free_context(context);
		return DB_ERROR;
	}
	
	if (sqlite3_prepare_v2(context->db, "select hash, salt from auth where username=?;", -1, &context->passquery, NULL) != SQLITE_OK) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to compile password prepared statement.");
		
		sqlite3_finalize(context->passquery);
		sqlite3_close(context->db);
		free_context(context);
		return DB_ERROR;
	}
	
	mosquitto_log_printf(MOSQ_LOG_INFO, "AutoHome authorization plugin initialized successfully");
	
	return SUCCESS;
}

/// @brief Plugin shut down routine
/// 
/// Close the connection to the SQLite database.
/// 
/// @param[in] user_data Plugin context.
/// @param[in] auth_opts Configuration options.
/// @param[in] auth_opt_count Number of configuration options.
/// @return Return code. On success, zero; otherwise, a number greater than zero.
int mosquitto_auth_plugin_cleanup(void *user_data, struct mosquitto_auth_opt *auth_opts, int auth_opt_count)
{
	Context* context = (Context*) user_data;
	
	if (sqlite3_finalize(context->passquery) != SQLITE_OK) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to finalize password prepared statement.");
		
		sqlite3_close(context->db);
		free_context(context);
		return DB_FILE_CANTCLOSE;
	}
	
	if (sqlite3_close(context->db) != SQLITE_OK) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to close SQLite database.");
		
		free_context(context);
		return DB_FILE_CANTCLOSE;
	}
	
	if (sqlite3_shutdown() != SQLITE_OK) {
		mosquitto_log_printf(MOSQ_LOG_ERR, "Failed to shutdown SQLite3.");
		
		free_context(context);
		return FAILED_SQLITE;
	}
	
	free_context(context);
	
	mosquitto_log_printf(MOSQ_LOG_INFO, "AutoHome authorization plugin shut down successfully");
	
	return SUCCESS;
}

/// @brief Security initialization routine
/// 
/// Additional initialization steps run after the plugin initialization.
/// Unlike the plugin initialization, this functions will be called again
/// every time the broker reloads its configuration while running.
/// Does nothing.
/// 
/// @param[in] user_data Plugin context.
/// @param[in] auth_opts Configuration options.
/// @param[in] auth_opt_count Number of configuration options.
/// @param[in] reload When the broker is asked to reload its configuration, it will run this
///                   routine again, with this flag as true; otherwise (on startup) it will be false.
/// @return Return code. On success, zero; otherwise, a number greater than zero.
int mosquitto_auth_security_init(void *user_data, struct mosquitto_auth_opt *auth_opts, int auth_opt_count, bool reload)
{
	return SUCCESS;
}

/// @brief Security shut down routine
/// 
/// Additional shut down steps run before the plugin shut down.
/// Unlike the plugin shut down, this functions will be called again
/// every time the broker reloads its configuration while running.
/// Does nothing.
/// 
/// @param[in] user_data Plugin context.
/// @param[in] auth_opts Configuration options.
/// @param[in] auth_opt_count Number of configuration options.
/// @param[in] reload When the broker is asked to reload its configuration, it will run this
///                   routine again, with this flag as true; otherwise (on startup) it will be false.
/// @return Return code. On success, zero; otherwise, a number greater than zero.
int mosquitto_auth_security_cleanup(void *user_data, struct mosquitto_auth_opt *auth_opts, int auth_opt_count, bool reload)
{
	return SUCCESS;
}

/// @brief Access control list check
/// 
/// Check whether a user has permission to read or write to a topic.
/// In this plugin, every user have read and write access to __username/\#__.
/// 
/// @param[in] user_data Plugin context.
/// @param[in] clientid Client's unique identification string, used to route messages.
///                     It must be the same as the username, to simplify authorization.
/// @param[in] username Client's username, used to authenticate them and select
///                     the topics it may be authorized to interact in.
/// @param[in] topic Topic the user is trying to access.
/// @param[in] access Type of acces the user is requesting: MOSQ_ACL_READ for reading,
///                   MOSQ_ACL_WRITE for writing.
/// @return Return code. MOSQ_ERR_SUCCESS on success, MOSQ_ERR_ACL_DENIED if access was
///         not granted and MOSQ_ERR_ACL_UNKNOWN if an application-specific error occurred.
int mosquitto_auth_acl_check(void *user_data, const char *clientid, const char *username, const char *topic, int access)
{
	Context* context = (Context*) user_data;
	
	if (clientid == NULL || username == NULL) {
		mosquitto_log_printf(MOSQ_LOG_NOTICE, "Bad username");
		return MOSQ_ERR_ACL_DENIED;
	}
	
	if (context->superuser != NULL && strcmp(context->superuser, username) == 0) {
		return MOSQ_ERR_SUCCESS;
	}
	
	if (strcmp(clientid, username) != 0) {
		mosquitto_log_printf(MOSQ_LOG_NOTICE, "Unauthorized access: ClientID != Username.");
		return MOSQ_ERR_ACL_DENIED;
	}
	
	size_t namelen  = strlen(username);
	size_t topiclen = strlen(topic);
	
	if (topiclen < namelen + 2) {  // at least 'username/x' long
		return MOSQ_ERR_ACL_DENIED;
	}
	
	int i;
	
	for (i = 0; i < namelen; i++) {
		if (username[i] != topic[i]) {
			return MOSQ_ERR_ACL_DENIED;
		}
	}
	
	if (topic[i] != '/') {
		return MOSQ_ERR_ACL_DENIED;
	}
	
	return MOSQ_ERR_SUCCESS;
}

/// @brief Username-password check
/// 
/// Check whether the provided password is correct for the given username.
/// If the username exists on the database, the password must match the stored one
/// (for security, only a hash of the password is stored).
/// If the username does not exist, the password must be empty.
/// 
/// @param[in] user_data Plugin context.
/// @param[in] username Client's username, public and permanent identification.
/// @param[in] password Client's password, hidden secret to prove their authenticity.
/// @return Return code. MOSQ_ERR_SUCCESS on success, MOSQ_ERR_AUTH if the authentication
///         failed and MOSQ_ERR_UNKNOWN if an application-specific error ocurred.
int mosquitto_auth_unpwd_check(void *user_data, const char *username, const char *password)
{
	Context* context = (Context*) user_data;
	
	char clienthash[65];
	
	char hash[65];
	char salt[65];
	
	if (username == NULL) {
		return MOSQ_ERR_AUTH;
	}
	
	if (retrieve_password(context->passquery, username, hash, 65, salt, 65)) {
		mosquitto_log_printf(MOSQ_LOG_WARNING, "Internal SQLite error, authentication cancelled.");
		return MOSQ_ERR_UNKNOWN;
	}
	
	if (strlen(hash) == 0) {  // unrecognized user
		return ((context->guestsecret == NULL && password == NULL) ||
		        (context->guestsecret != NULL && password != NULL && strcmp(context->guestsecret, password) == 0)) ?
		            MOSQ_ERR_SUCCESS : MOSQ_ERR_AUTH;
	}
	
	sha256_ctx hashctx;
	
	sha256_init(&hashctx);
	sha256_update(&hashctx, (unsigned char*) salt, strlen(salt));
	sha256_update(&hashctx, (unsigned char*) password, strlen(password));
	sha256_final(&hashctx,  (unsigned char*) clienthash);
	
	char digit[17] = "0123456789abcdef";
	
	// encode as a human-readable base16 string
	for (int i = 31, k = 63; i >= 0; i--, k -= 2) {
		char byte = clienthash[i];
		
		clienthash[k]     = digit[byte        & 0x0f];
		clienthash[k - 1] = digit[(byte >> 4) & 0x0f];
	}
	
	clienthash[64] = 0;
	
	return (strcmp(clienthash, hash) == 0) ? MOSQ_ERR_SUCCESS : MOSQ_ERR_AUTH;
}

/// @brief PSK key retrieval routine
/// 
/// Retrieve the PSK secret key associated with the given client.
/// Not implemented.
/// 
/// @param[in] user_data Plugin context.
/// @param[in] hint Associated PSK hint.
/// @param[in] identity Client's identity claim.
/// @param[out] key Retrieved PSK key.
/// @param[in] max_key_len Maximum size of the key.
/// @return Return code. On success, zero; otherwise, a number greater than zero.
///         If the function is not required, also a number greater than zero.
int mosquitto_auth_psk_key_get(void *user_data, const char *hint, const char *identity, char *key, int max_key_len)
{
	return NOTREQUIRED;
}
