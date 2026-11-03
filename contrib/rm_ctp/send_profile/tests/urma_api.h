/* CPU fixture ONLY. Never used for node builds or hardware/ABI acceptance. */
#include <stdint.h>
#include <stddef.h>
typedef int urma_status_t;
#define URMA_SUCCESS 0
#define URMA_EAGAIN 11
#define URMA_TM_RM 1
#define URMA_OPC_SEND 2
#define URMA_CR_SUCCESS 0
typedef struct {uint64_t addr; uint32_t len;} urma_sge_t;
typedef struct {struct {struct {int trans_mode;} jfs_cfg;} jetty_cfg;} urma_jetty_t;
typedef struct urma_jfs_wr {
 int opcode;
 struct {struct {unsigned inline_flag;unsigned complete_enable;} bs;} flag;
 struct {struct {unsigned num_sge;urma_sge_t *sge;} src;} send;
 struct urma_jfs_wr *next;
} urma_jfs_wr_t;
typedef struct {int unused;} urma_jfc_t;
typedef struct {int status;} urma_cr_t;
urma_status_t urma_post_jetty_send_wr(urma_jetty_t *,urma_jfs_wr_t *,urma_jfs_wr_t **);
int urma_poll_jfc(urma_jfc_t *,int,urma_cr_t *);
