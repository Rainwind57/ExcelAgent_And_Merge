{
    "nodeData": {
        "62JcUumQRQucgR4ExaYwZ6": {
            "Type": "TaskNode",
            "Pos": [
                -320.0,
                -48.0
            ],
            "Data": {
                "nodeData": {
                    "6XAHi5SsdGUZhVKBuJuLbd": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            -160.0,
                            -224.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "message": "",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "6XAHi5SsdGUZhVKBuJuLbd"
                    },
                    "MhPZMuZWzpg8BYdGJVnc9L": {
                        "Type": "FantasyStartNode",
                        "Pos": [
                            -398.0,
                            -220.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "Type": "FantasyStartNode"
                        },
                        "NodeID": "MhPZMuZWzpg8BYdGJVnc9L"
                    },
                    "TAJLSQ9pzFGfMrVsEXDbhB": {
                        "Type": "FantasyEndNode",
                        "Pos": [
                            64.0,
                            -240.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "Type": "FantasyEndNode"
                        },
                        "NodeID": "TAJLSQ9pzFGfMrVsEXDbhB"
                    }
                },
                "lineData": [
                    {
                        "startNode": "6XAHi5SsdGUZhVKBuJuLbd",
                        "endNode": "TAJLSQ9pzFGfMrVsEXDbhB",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "MhPZMuZWzpg8BYdGJVnc9L",
                        "endNode": "6XAHi5SsdGUZhVKBuJuLbd",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    }
                ],
                "groupData": {},
                "baseProp": {
                    "taskId": 901001,
                    "taskname": "\u5267\u60c5\u4efb\u52a1\u6d4b\u8bd51",
                    "hint": "\u6d4b\u8bd5\u5267\u60c5",
                    "desc": "\u5267\u60c5\u4efb\u52a1\u6d4b\u8bd51",
                    "Type": "BaseTaskComponent"
                },
                "taskTargetProp": {
                    "target_type": "NONE",
                    "Type": "TaskTargetComponent"
                },
                "exclusiveProp": {
                    "target_movie": 901001,
                    "Type": "ExclusivePropComponent"
                },
                "actionProp": {
                    "npc_ids": [],
                    "reward_ids": [],
                    "Type": "EventComponent"
                },
                "nextTaskProp": {
                    "next_tasks": [
                        "901002"
                    ],
                    "Type": "NextTaskComponent"
                }
            },
            "ParentGroupID": "",
            "Name": "\u8d77\u59cb\u4efb\u52a1(901001): \u5267\u60c5\u4efb\u52a1\u6d4b\u8bd51",
            "isStart": true,
            "isEnd": false,
            "NodeID": "62JcUumQRQucgR4ExaYwZ6"
        },
        "wK8dhAHPYfi6tw3aNBWRxb": {
            "Type": "TaskNode",
            "Pos": [
                32.0,
                -48.0
            ],
            "Data": {
                "nodeData": {
                    "4VmKCc2Dkwhx36YDddvdRf": {
                        "Type": "FantasyDelayNode",
                        "Pos": [
                            64.0,
                            192.0
                        ],
                        "Data": {
                            "delay": 1.0,
                            "Type": "FantasyDelayNode"
                        },
                        "NodeID": "4VmKCc2Dkwhx36YDddvdRf"
                    },
                    "62a4zt5CqA7zzPTj6c5neF": {
                        "Type": "FantasySwitchNode",
                        "Pos": [
                            400.0,
                            896.0
                        ],
                        "Data": {
                            "Type": "FantasySwitchNode"
                        },
                        "NodeID": "62a4zt5CqA7zzPTj6c5neF"
                    },
                    "Adxg9WyuLnKuaVydAWjfdd": {
                        "Type": "FantasyRandIntNode",
                        "Pos": [
                            192.0,
                            896.0
                        ],
                        "Data": {
                            "min": 0,
                            "max": 1,
                            "Type": "FantasyRandIntNode"
                        },
                        "NodeID": "Adxg9WyuLnKuaVydAWjfdd"
                    },
                    "Dca7Nj2dmuQDXuCuSyoVVB": {
                        "Type": "FantasyEndNode",
                        "Pos": [
                            -256.0,
                            1072.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "Type": "FantasyEndNode"
                        },
                        "NodeID": "Dca7Nj2dmuQDXuCuSyoVVB"
                    },
                    "Ef9PDTYVPCqzCDTLyaU3LQ": {
                        "Type": "FantasyLoopMarkNode",
                        "Pos": [
                            832.0,
                            992.0
                        ],
                        "Data": {
                            "mark_name": "bbb",
                            "Type": "FantasyLoopMarkNode"
                        },
                        "NodeID": "Ef9PDTYVPCqzCDTLyaU3LQ"
                    },
                    "Jgpt6hChNa7mjm3xTCZMpT": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            -176.0,
                            192.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "message": "\u7b2c\u4e00\u6761\u6d4b\u8bd5\u6d88\u606f",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "Jgpt6hChNa7mjm3xTCZMpT"
                    },
                    "LUkSpLv7xJnGNAiCyg3Emk": {
                        "Type": "FantasyRandIntNode",
                        "Pos": [
                            -256.0,
                            800.0
                        ],
                        "Data": {
                            "min": 0,
                            "max": 1,
                            "Type": "FantasyRandIntNode"
                        },
                        "NodeID": "LUkSpLv7xJnGNAiCyg3Emk"
                    },
                    "PtDSqQNfN7Lm9QKTQQmiVN": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            192.0,
                            784.0
                        ],
                        "Data": {
                            "message": "\u5192\u6ce1\u63d0\u793a1",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "PtDSqQNfN7Lm9QKTQQmiVN"
                    },
                    "USpqrtGgXiTd6rcrSNUeKC": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            624.0,
                            992.0
                        ],
                        "Data": {
                            "message": "\u5192\u6ce1\u63d0\u793a3",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "USpqrtGgXiTd6rcrSNUeKC"
                    },
                    "W2YDForDLGSnCbeEVqTa7L": {
                        "Type": "FantasySwitchNode",
                        "Pos": [
                            -64.0,
                            432.0
                        ],
                        "Data": {
                            "Type": "FantasySwitchNode"
                        },
                        "NodeID": "W2YDForDLGSnCbeEVqTa7L"
                    },
                    "d8noh6dMtkHttDkJCCnFLj": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            624.0,
                            880.0
                        ],
                        "Data": {
                            "message": "\u5192\u6ce1\u63d0\u793a2",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "d8noh6dMtkHttDkJCCnFLj"
                    },
                    "daowxZhn6YNVmdmV769cB2": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            192.0,
                            496.0
                        ],
                        "Data": {
                            "message": "\u5192\u6ce1\u63d0\u793a2",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "daowxZhn6YNVmdmV769cB2"
                    },
                    "gs4qHjF7CQs64Fykq8dkLX": {
                        "Type": "FantasyLoopMarkNode",
                        "Pos": [
                            832.0,
                            880.0
                        ],
                        "Data": {
                            "mark_name": "aaa",
                            "Type": "FantasyLoopMarkNode"
                        },
                        "NodeID": "gs4qHjF7CQs64Fykq8dkLX"
                    },
                    "hk3fzUTqoThWan6rCAFstd": {
                        "Type": "FantasyRandIntNode",
                        "Pos": [
                            -336.0,
                            432.0
                        ],
                        "Data": {
                            "min": 0,
                            "max": 2,
                            "Type": "FantasyRandIntNode"
                        },
                        "NodeID": "hk3fzUTqoThWan6rCAFstd"
                    },
                    "iLZZYvhh3ACftWX7eV63b3": {
                        "Type": "FantasySwitchNode",
                        "Pos": [
                            -48.0,
                            800.0
                        ],
                        "Data": {
                            "Type": "FantasySwitchNode"
                        },
                        "NodeID": "iLZZYvhh3ACftWX7eV63b3"
                    },
                    "ioAAFTQ2HGnqoxdafJJgB4": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            192.0,
                            608.0
                        ],
                        "Data": {
                            "message": "\u5192\u6ce1\u63d0\u793a3",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "ioAAFTQ2HGnqoxdafJJgB4"
                    },
                    "jfKtdCvkW5rGqnwV5kdG73": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            512.0,
                            496.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "message": "\u7b2c\u4e09\u6761\u6d4b\u8bd5\u6d88\u606f",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "jfKtdCvkW5rGqnwV5kdG73"
                    },
                    "m3Fks3zdAbX4VvZc3o8Z3F": {
                        "Type": "FantasyStartNode",
                        "Pos": [
                            -672.0,
                            800.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "Type": "FantasyStartNode"
                        },
                        "NodeID": "m3Fks3zdAbX4VvZc3o8Z3F"
                    },
                    "n6yHXv9oyYdoYTNftx3rG9": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            304.0,
                            192.0
                        ],
                        "Data": {
                            "delay": 0.0,
                            "message": "\u7b2c\u4e8c\u6761\u6d4b\u8bd5\u6d88\u606f",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "n6yHXv9oyYdoYTNftx3rG9"
                    },
                    "pQZvFmYXJEjwE9S5TJbWYc": {
                        "Type": "FantasyLoopStartNode",
                        "Pos": [
                            -464.0,
                            800.0
                        ],
                        "Data": {
                            "Type": "FantasyLoopStartNode",
                            "finish_condition": "ALL",
                            "specified_mark_names": []
                        },
                        "NodeID": "pQZvFmYXJEjwE9S5TJbWYc"
                    },
                    "tEfQmBa29j7zpjzYoxYGEa": {
                        "Type": "FantasyShowPopupMessageNode",
                        "Pos": [
                            192.0,
                            400.0
                        ],
                        "Data": {
                            "message": "\u5192\u6ce1\u63d0\u793a1",
                            "Type": "FantasyShowPopupMessageNode"
                        },
                        "NodeID": "tEfQmBa29j7zpjzYoxYGEa"
                    },
                    "x7b7xmNE27YFwHzKB9A2PL": {
                        "Type": "FantasyLoopMarkNode",
                        "Pos": [
                            400.0,
                            784.0
                        ],
                        "Data": {
                            "mark_name": "aaa",
                            "Type": "FantasyLoopMarkNode"
                        },
                        "NodeID": "x7b7xmNE27YFwHzKB9A2PL"
                    }
                },
                "lineData": [
                    {
                        "startNode": "4VmKCc2Dkwhx36YDddvdRf",
                        "endNode": "n6yHXv9oyYdoYTNftx3rG9",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "62a4zt5CqA7zzPTj6c5neF",
                        "endNode": "d8noh6dMtkHttDkJCCnFLj",
                        "startPort": "0",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "62a4zt5CqA7zzPTj6c5neF",
                        "endNode": "USpqrtGgXiTd6rcrSNUeKC",
                        "startPort": "1",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "Adxg9WyuLnKuaVydAWjfdd",
                        "endNode": "62a4zt5CqA7zzPTj6c5neF",
                        "startPort": "rand_int",
                        "endPort": "index"
                    },
                    {
                        "startNode": "Jgpt6hChNa7mjm3xTCZMpT",
                        "endNode": "4VmKCc2Dkwhx36YDddvdRf",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "LUkSpLv7xJnGNAiCyg3Emk",
                        "endNode": "iLZZYvhh3ACftWX7eV63b3",
                        "startPort": "rand_int",
                        "endPort": "index"
                    },
                    {
                        "startNode": "PtDSqQNfN7Lm9QKTQQmiVN",
                        "endNode": "x7b7xmNE27YFwHzKB9A2PL",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "USpqrtGgXiTd6rcrSNUeKC",
                        "endNode": "Ef9PDTYVPCqzCDTLyaU3LQ",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "W2YDForDLGSnCbeEVqTa7L",
                        "endNode": "tEfQmBa29j7zpjzYoxYGEa",
                        "startPort": "0",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "W2YDForDLGSnCbeEVqTa7L",
                        "endNode": "daowxZhn6YNVmdmV769cB2",
                        "startPort": "1",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "W2YDForDLGSnCbeEVqTa7L",
                        "endNode": "ioAAFTQ2HGnqoxdafJJgB4",
                        "startPort": "2",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "d8noh6dMtkHttDkJCCnFLj",
                        "endNode": "gs4qHjF7CQs64Fykq8dkLX",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "daowxZhn6YNVmdmV769cB2",
                        "endNode": "jfKtdCvkW5rGqnwV5kdG73",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "hk3fzUTqoThWan6rCAFstd",
                        "endNode": "W2YDForDLGSnCbeEVqTa7L",
                        "startPort": "rand_int",
                        "endPort": "index"
                    },
                    {
                        "startNode": "iLZZYvhh3ACftWX7eV63b3",
                        "endNode": "PtDSqQNfN7Lm9QKTQQmiVN",
                        "startPort": "0",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "iLZZYvhh3ACftWX7eV63b3",
                        "endNode": "Adxg9WyuLnKuaVydAWjfdd",
                        "startPort": "1",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "ioAAFTQ2HGnqoxdafJJgB4",
                        "endNode": "jfKtdCvkW5rGqnwV5kdG73",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "m3Fks3zdAbX4VvZc3o8Z3F",
                        "endNode": "pQZvFmYXJEjwE9S5TJbWYc",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "pQZvFmYXJEjwE9S5TJbWYc",
                        "endNode": "Dca7Nj2dmuQDXuCuSyoVVB",
                        "startPort": "Completed",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "pQZvFmYXJEjwE9S5TJbWYc",
                        "endNode": "LUkSpLv7xJnGNAiCyg3Emk",
                        "startPort": "Loop",
                        "endPort": "__in__"
                    },
                    {
                        "startNode": "tEfQmBa29j7zpjzYoxYGEa",
                        "endNode": "jfKtdCvkW5rGqnwV5kdG73",
                        "startPort": "__out__",
                        "endPort": "__in__"
                    }
                ],
                "groupData": {},
                "baseProp": {
                    "taskId": "901002",
                    "taskname": "\u5267\u60c5\u4efb\u52a1\u6d4b\u8bd52",
                    "hint": "\u6d4b\u8bd5\u5267\u60c5",
                    "desc": "\u5267\u60c5\u4efb\u52a1\u6d4b\u8bd52",
                    "Type": "BaseTaskComponent"
                },
                "taskTargetProp": {
                    "target_type": "NONE",
                    "Type": "TaskTargetComponent"
                },
                "exclusiveProp": {
                    "target_movie": 0,
                    "Type": "ExclusivePropComponent"
                },
                "actionProp": {
                    "npc_ids": [],
                    "reward_ids": [],
                    "Type": "EventComponent"
                },
                "nextTaskProp": {
                    "next_tasks": [],
                    "Type": "NextTaskComponent"
                }
            },
            "ParentGroupID": "",
            "Name": "\u8fde\u7eed\u4efb\u52a1(901002): \u5267\u60c5\u4efb\u52a1\u6d4b\u8bd52",
            "isStart": false,
            "isEnd": false,
            "NodeID": "wK8dhAHPYfi6tw3aNBWRxb"
        }
    },
    "lineData": [
        {
            "startNode": "62JcUumQRQucgR4ExaYwZ6",
            "endNode": "wK8dhAHPYfi6tw3aNBWRxb",
            "startPort": "__out2__",
            "endPort": "__in__"
        }
    ],
    "version": 2.0,
    "groupData": {}
}