/* Reuse the accepted direct-UCT bootstrap and resource setup. Its WRITE main
 * is renamed, never called. All business data below travels through GET_ZCOPY. */
#define main unused_native_write_probe_main
#include "../native_write/write_probe.c"
#undef main

enum { READ_REGION = 8192, READ_GUARD = 128, READ_ALLOCATION = 8448 };
static unsigned read_completions;

static unsigned char read_pattern(unsigned round, size_t index)
{
    return (unsigned char)(round * 31u + index * 13u + (index >> 8));
}

static void read_complete(uct_completion_t *comp)
{
    require(comp->status == UCS_OK, "READ completion status");
    ++read_completions;
}

int main(int argc, char **argv)
{
    state_t s = {0};
    uct_md_attr_t mdattr;
    uct_mem_h mem;
    uct_rkey_bundle_t key;
    uct_ep_h ep = NULL;
    uct_ep_params_t params = {0};
    uct_iov_t iov = {0};
    unsigned char local[560] = {0}, remote[560] = {0};
    unsigned char packed[512] = {0}, peer_key[512] = {0};
    unsigned char *buffer;
    uint64_t address = 0;
    const size_t sizes[] = {8, 64, 4096, 8, 64, 4096};
    const size_t offsets[] = {0, 0, 0, 8, 64, 128};
    int provider, fd;
    char ack;
    ucs_status_t status;

    require(argc == 5, "READ probe listen|connect IP PORT MAPS");
    provider = !strcmp(argv[1], "listen");
    require(provider || !strcmp(argv[1], "connect"), "READ role");
    s.role = provider ? "provider" : "reader";
    setvbuf(stdout, NULL, _IOLBF, 0);
    fd = socket_open(provider, argv[2], strtoul(argv[3], NULL, 10));
    s.deadline = seconds() + 70;
    open_iface(&s);
    require(s.attr.cap.flags & UCT_IFACE_FLAG_GET_ZCOPY, "native GET_ZCOPY capability");
    require(!(s.attr.cap.flags & (UCT_IFACE_FLAG_GET_BCOPY | UCT_IFACE_FLAG_PUT_ZCOPY)),
            "READ-only probe capabilities");
    require(s.attr.cap.get.max_iov == 1 && s.attr.cap.get.max_zcopy >= 4096,
            "READ capability limit");
    checked(uct_md_query(s.md, &mdattr), "READ MD query");
    require(mdattr.rkey_packed_size <= sizeof(packed), "READ key capacity");
    buffer = malloc(READ_ALLOCATION);
    require(buffer != NULL, "READ memory allocation");
    checked(uct_md_mem_reg(s.md, buffer, READ_ALLOCATION, UCT_MD_MEM_ACCESS_ALL, &mem),
            "READ register");
    checked(uct_md_mkey_pack(s.md, mem, packed), "READ key pack");
    checked(uct_iface_get_device_address(s.iface, (uct_device_addr_t *)local), "READ device addr");
    checked(uct_iface_get_address(s.iface, (uct_iface_addr_t *)(local + 512)), "READ iface addr");
    if (provider) {
        address = (uint64_t)(buffer + READ_GUARD);
        bytes(fd, local, sizeof(local), 1);
        bytes(fd, packed, sizeof(packed), 1);
        bytes(fd, &address, sizeof(address), 1);
        bytes(fd, remote, sizeof(remote), 0);
    } else {
        bytes(fd, remote, sizeof(remote), 0);
        bytes(fd, peer_key, sizeof(peer_key), 0);
        bytes(fd, &address, sizeof(address), 0);
        bytes(fd, local, sizeof(local), 1);
        checked(uct_rkey_unpack(s.component, peer_key, &key), "READ remote key unpack");
        params.field_mask = UCT_EP_PARAM_FIELD_IFACE | UCT_EP_PARAM_FIELD_DEV_ADDR |
                            UCT_EP_PARAM_FIELD_IFACE_ADDR;
        params.iface = s.iface;
        params.dev_addr = (uct_device_addr_t *)remote;
        params.iface_addr = (uct_iface_addr_t *)(remote + 512);
        checked(uct_ep_create(&params, &ep), "create READ EP");
        iov.memh = mem;
        iov.count = 1;
    }
    for (unsigned round = 0; round < 6; ++round) {
        size_t n = sizes[round], offset = offsets[round];
        memset(buffer, 0xa5, READ_ALLOCATION);
        if (provider) {
            for (size_t i = 0; i < READ_REGION; ++i) {
                buffer[READ_GUARD + i] = read_pattern(round, i);
            }
            ack = 'R';
            bytes(fd, &ack, 1, 1);
            marker(&s, fd, 'V');
            for (size_t i = 0; i < READ_ALLOCATION; ++i) {
                unsigned char expected = (i >= READ_GUARD && i < READ_GUARD + READ_REGION) ?
                                         read_pattern(round, i - READ_GUARD) : 0xa5;
                require(buffer[i] == expected, "READ provider source/guards unchanged");
            }
            ack = 'U';
            bytes(fd, &ack, 1, 1);
        } else {
            uct_completion_t comp = { .func = read_complete, .count = 1, .status = UCS_OK };
            unsigned previous = read_completions;
            marker(&s, fd, 'R');
            iov.buffer = buffer + READ_GUARD + offset;
            iov.length = n;
            do {
                status = uct_ep_get_zcopy(ep, &iov, 1, address + offset, key.rkey, &comp);
                require(status == UCS_OK || status == UCS_INPROGRESS ||
                        status == UCS_ERR_NO_RESOURCE, "native READ post");
                if (status == UCS_ERR_NO_RESOURCE) {
                    tick(&s);
                }
            } while (status == UCS_ERR_NO_RESOURCE);
            if (status == UCS_INPROGRESS) {
                while (comp.count != 0) {
                    tick(&s);
                }
                require(read_completions == previous + 1, "one READ completion");
            }
            require(comp.status == UCS_OK, "READ result status");
            flush(&s);
            for (size_t i = 0; i < READ_ALLOCATION; ++i) {
                unsigned char expected = (i >= READ_GUARD + offset && i < READ_GUARD + offset + n) ?
                                         read_pattern(round, i - READ_GUARD) : 0xa5;
                require(buffer[i] == expected, "READ destination bytes and full guards");
            }
            ack = 'V';
            bytes(fd, &ack, 1, 1);
            marker(&s, fd, 'U');
        }
        printf("NATIVE_READ_ROUND=%u BYTES=%zu OFFSET=%zu ROLE=%s DATA=PASS\n",
               round, n, offset, s.role);
    }
    flush(&s);
    maps(argv[4]);
    if (!provider) {
        uct_ep_destroy(ep);
        checked(uct_rkey_release(s.component, &key), "READ remote key release");
    }
    checked(uct_md_mem_dereg(s.md, mem), "READ deregister");
    free(buffer);
    uct_iface_close(s.iface);
    uct_md_close(s.md);
    uct_worker_destroy(s.worker);
    ucs_async_context_destroy(s.async);
    close(fd);
    puts("NATIVE_READ_PASS ROUNDS=6 SIZE=8/64/4096 GUARDS=PASS REGISTRATION_RELEASE=PASS");
    return 0;
}
