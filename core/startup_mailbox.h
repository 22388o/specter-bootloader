/**
 * @file       startup_mailbox.h
 * @brief      Mailbox used to pass parameters to the Bootloader
 * @author     Mike Tolkachev <contact@miketolkachev.dev>
 * @copyright  Copyright 2020 Crypto Advance GmbH. All rights reserved.
 */

#ifndef STARTUP_MAILBOX_H_INCLUDED
/// Avoids multiple inclusion of this file
#define STARTUP_MAILBOX_H_INCLUDED

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "bootloader.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Reads structure with arguments from the Start-up Mailbox
 *
 * @param p_destination  destination structure receiving data from the mailbox
 * @return               true if successful
 */
bool bl_read_args(bl_args_t* p_destination);

/**
 * Writes structure with arguments to the Start-up Mailbox
 *
 * @param p_source  source structure written to the mailbox
 * @return          true if successful
 */
bool bl_write_args(const bl_args_t* p_source);

#ifdef __cplusplus
} // extern "C"
#endif

#endif // STARTUP_MAILBOX_H_INCLUDED
