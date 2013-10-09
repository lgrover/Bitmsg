from collections import deque
import gzip
import os
import sys
import time
import traceback

import base58
from bitcoin import Bitcoin
from common import *
from network import BitcoinNetwork
from transaction import Transaction

class Callbacks:
    TX_TIMEOUT = 4 * 60 * 60

    def __init__(self):
        self.watched_addresses = {}
        self.seen_transactions = set()
        self.seen_transactions_timeout = deque()

    def watch_public(self):
        # Watch the public-message address
        key = b'\x00'
        address = addressgen.generate_address_from_data(key, version=0)
        self.watched_addresses[address] = (ENCRYPT_NONE, key)

    def watch_rc4(self, key):
        # Hash the key and add to watched addresses
        address = addressgen.generate_address_from_data(key, version=0)
        self.watched_addresses[address] = (ENCRYPT_RC4, key)

    def will_request_transaction(self, txhash):
        now = time.time()

        while len(self.seen_transactions_timeout) and now > (self.seen_transactions_timeout[0][1] + Callbacks.TX_TIMEOUT):
            tx_hash, _ = self.seen_transactions_timeout.popleft()
            self.seen_transactions.pop(tx_hash)

        return txhash not in self.seen_transactions

    def got_transaction(self, tx):
        # Remember that we got this transaction for a little while
        now = time.time()
        tx_hash = tx.hash()
        self.seen_transactions.add(tx_hash)
        self.seen_transactions_timeout.append((tx_hash, now))

        # Check first output to see if it's delivered to the encryption address,
        # then check second and third address to see if it's something bound for
        # us. (If it's the third one, then the 2nd address is change).  The rest
        # of the addresses are part of the payload.
        if len(tx.outputs) < 3:
            return

        output0 = tx.outputs[0]
        if output0.getBitcoinAddress() != MESSAGE_ADDRESS_TRIGGER:
            return

        print('tx {} is for bitmsg'.format(Bitcoin.bytes_to_hexstring(tx_hash)))

        msg_start_n = 1
        while True:
            if msg_start_n >= len(tx.outputs):
                # Invalid message
                return

            msg_start = tx.outputs[msg_start_n]
            msg_start_n += 1

            v = self.watched_addresses.get(msg_start.getBitcoinAddress(), None)
            if v is not None:
                algorithm, key = v
                break

        # build the msg content
        header = None
        msg = []
        for k in range(msg_start_n, len(tx.outputs)):
            output = tx.outputs[k]
            payload = base58.decode_to_bytes(output.getBitcoinAddress())[1:-4]

            if k == msg_start_n:
                # First five bytes are the header
                header, payload = payload[:5], payload[5:]

                if (header[1] & 0x7f) != algorithm:
                    # We can't decrypt this, says the header. The encryption algorithm doesn't match.
                    return

                if header[4] != 0xff:
                    # TODO - handle reserved bits
                    return

            if k == len(tx.outputs) - 1:
                if header[3] != 0:
                    if header[3] >= PIECE_SIZE:
                        # Invalid padding
                        return
                    payload = payload[:-header[3]]
                pass

            msg.append(payload)

        if header is None:
            return

        msg = b''.join(msg)
        decrypted_message = decrypt(key, msg, algorithm)

        if header[1] & 0x80:
            # Message is compressed
            decrypted_message = gzip.decompress(decrypted_message)

        print('-----Begin message to {}-----'.format(msg_start.getBitcoinAddress()))
        try:
            sys.stdout.write(decrypted_message.decode('utf8'))
        except UnicodeDecodeError:
            sys.stdout.write(repr(decrypted_message))
        print('\n-----End message-----')

def main():
    cb = Callbacks()

    # Handle some simple command-line arguments
    # -w Key : watch an RC4-encrypted channel
    # -p     : watch the Public unencrypted channel
    # -t tx  : try decoding and processing transaction 'tx' (hex)
    i = 1
    done = False
    while i < len(sys.argv):
        c = sys.argv[i]
        if c == '-w':
            i += 1
            cb.watch_rc4(sys.argv[i].encode('utf8'))
        elif c == '-p':
            cb.watch_public()
        elif c == '-t':
            i += 1
            cb.got_transaction(Transaction.unserialize(Bitcoin.hexstring_to_bytes(sys.argv[i], reverse=False))[0])
            done = True
        else:
            print('invalid command line argument: {}'.format(c))
            return
        i += 1

    if done:
        return

    print('The Bitmsg trigger address is {}'.format(MESSAGE_ADDRESS_TRIGGER))

    # start network thread
    bitcoin_network = BitcoinNetwork(cb)
    bitcoin_network.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bitcoin_network.stop()
        bitcoin_network.join()
        raise

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit, Exception):
        traceback.print_exc()
