/* Test only. Reuse the accepted public-UCT bootstrap and payload checker.
 * Inject at receive decoding, never fabricate a provider CQE. */
#include <uct/api/uct.h>
#include <stdio.h>
#include <string.h>
static uct_ep_h failed_eps[2];
static unsigned error_calls;
static ucs_status_t error_status;

static ucs_status_t on_error(void *arg, uct_ep_h ep, ucs_status_t status)
{
    (void)arg;
    if (error_calls >= 2 || status != UCS_ERR_ENDPOINT_TIMEOUT ||
        (error_calls && failed_eps[0] == ep)) {
        fputs("FAILURE_PROBE_BAD_CALLBACK\n", stderr);
        return UCS_ERR_IO_ERROR;
    }
    failed_eps[error_calls++] = ep;
    error_status = status;
    return UCS_OK;
}

static ucs_status_t failure_iface_open(uct_md_h md, uct_worker_h worker,
        const uct_iface_params_t *params, const uct_iface_config_t *config,
        uct_iface_h *iface)
{
    uct_iface_params_t copy = *params;
    copy.field_mask |= UCT_IFACE_PARAM_FIELD_ERR_HANDLER |
                       UCT_IFACE_PARAM_FIELD_ERR_HANDLER_ARG |
                       UCT_IFACE_PARAM_FIELD_ERR_HANDLER_FLAGS;
    copy.err_handler = on_error;
    copy.err_handler_arg = NULL;
    copy.err_handler_flags = UCT_CB_FLAG_ASYNC;
    return uct_iface_open(md, worker, &copy, config, iface);
}

#define main accepted_normal_probe_main
#define uct_iface_open failure_iface_open
#include "../send_reliable/node_probe.c"
#undef uct_iface_open
#undef main

int main(int argc, char **argv)
{
    state_t s = {0};
    uct_ep_params_t params = {0};
    uct_ep_h eps[2] = {0};
    unsigned char local[560] = {0}, remote[560] = {0};
    uint32_t header, peer_header;
    message_t message;
    unsigned i;
    char marker_value = 'D';
    const char *fault = getenv("HUCX_SEND_FAILURE_PROFILE");
    int expected, server, fd;
    double begin, elapsed;
    ucs_status_t status;
    require(argc == 5 && fault != NULL, "failure probe arguments/profile");
    require(!strcmp(fault, "none") || !strcmp(fault, "drop_data") ||
            !strcmp(fault, "drop_ack"), "known fault profile");
    expected = strcmp(fault, "none") != 0;
    server = !strcmp(argv[1], "listen");
    setvbuf(stdout, NULL, _IOLBF, 0);
    fd = socket_open(server, argv[2], (unsigned)strtoul(argv[3], NULL, 10));
    s.role = server ? "receiver" : "sender";
    s.deadline = seconds() + 20;
    open_iface(&s);
    require(s.attr.device_addr_len <= 512, "device address length");
    checked(uct_iface_get_device_address(s.iface, (uct_device_addr_t *)local), "device address");
    checked(uct_iface_get_address(s.iface, (uct_iface_addr_t *)(local + 512)), "iface address");
    header = htonl((uint32_t)s.attr.device_addr_len);
    if (!server) {
        bytes(fd, &header, sizeof(header), 1);
        bytes(fd, local, sizeof(local), 1);
    }
    bytes(fd, &peer_header, sizeof(peer_header), 0);
    bytes(fd, remote, sizeof(remote), 0);
    require(ntohl(peer_header) == s.attr.device_addr_len, "peer device address length");
    if (server) {
        bytes(fd, &header, sizeof(header), 1);
        bytes(fd, local, sizeof(local), 1);
    }
    maps(argv[4]);
    begin = seconds();
    if (!server) {
        params.field_mask = UCT_EP_PARAM_FIELD_IFACE | UCT_EP_PARAM_FIELD_DEV_ADDR |
                            UCT_EP_PARAM_FIELD_IFACE_ADDR;
        params.iface = s.iface;
        params.dev_addr = (uct_device_addr_t *)remote;
        params.iface_addr = (uct_iface_addr_t *)(remote + 512);
        for (i = 0; i < 2; ++i) {
            create(&s, &params, &eps[i]);
        }
        fill(&message, 0, 0);
        do {
            status = post(eps[0], &message, 0);
            require(status == UCS_OK || status == UCS_ERR_NO_RESOURCE, "initial SEND");
            tick(&s);
        } while (status != UCS_OK);
        do {
            tick(&s);
            status = uct_iface_flush(s.iface, 0, NULL);
            require(status == UCS_OK || status == UCS_INPROGRESS ||
                    status == UCS_ERR_NO_RESOURCE || status == UCS_ERR_ENDPOINT_TIMEOUT,
                    "flush must report success or endpoint timeout");
        } while (status == UCS_INPROGRESS || status == UCS_ERR_NO_RESOURCE);
        elapsed = seconds() - begin;
        require(expected ? (status == UCS_ERR_ENDPOINT_TIMEOUT && error_calls == 2 &&
                            error_status == UCS_ERR_ENDPOINT_TIMEOUT) :
                           (status == UCS_OK && error_calls == 0), "expected callback/flush outcome");
        if (expected) {
            require((failed_eps[0] == eps[0] || failed_eps[0] == eps[1]) &&
                    (failed_eps[1] == eps[0] || failed_eps[1] == eps[1]), "callback EP identities");
            for (i = 0; i < 4; ++i) {
                require(post(eps[i % 2], &message, 0) == UCS_ERR_ENDPOINT_TIMEOUT,
                        "SEND must reject after terminal failure");
                require(uct_ep_flush(eps[i % 2], 0, NULL) == UCS_ERR_ENDPOINT_TIMEOUT,
                        "EP flush must retain terminal error");
                tick(&s);
            }
            require(error_calls == 2, "error callback exactly once per EP");
        }
        bytes(fd, &marker_value, 1, 1);
        marker(&s, fd, 'R');
        for (i = 0; i < 2; ++i) {
            uct_ep_destroy(eps[i]);
        }
    } else {
        marker(&s, fd, 'D');
        require(s.received == (!strcmp(fault, "drop_data") ? 0u : 1u),
                "receiver delivery count/content/order/dedup");
        require(error_calls == 0, "receiver has no application EP error callbacks");
        marker_value = 'R';
        bytes(fd, &marker_value, 1, 1);
        elapsed = seconds() - begin;
    }
    close_state(&s);
    close(fd);
    printf("SEND_FAILURE_PROBE_PASS ROLE=%s PROFILE=%s CALLBACKS=%u RECEIVED=%u "
           "ELAPSED=%.6f CLOSED=YES\n", s.role, fault, error_calls, s.received, elapsed);
    return 0;
}
