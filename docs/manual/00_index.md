# Crypto Trading Fleet — Maintenance Manual

_Last reviewed: 2026-06-24_

## Summary

This project is a small **fleet of automated crypto-trading "bots"** that Roy built. It is
deliberately running in **paper mode** (simulated money) plus a tiny "shadow" sample of
real trades — it is **not** trading meaningful real money yet, by design. The system runs
on a rented Linux server ("Hetzner"), watches crypto markets and wallets, and records how
it *would* have traded so its edge can be measured before any real capital is risked.

This manual exists so that **someone other than Roy can keep it alive and continue it.**
It is written in two layers: a plain-language **Operator track** for keeping things running,
and a technical **Engineer track** for understanding and changing the code.

## Who reads what

| You are… | Start here | Goal |
|---|---|---|
| **Keeping it alive** (non-technical) | Operator track — sections **1.x** | Know it's healthy, do the recurring tasks, react to alerts, don't break anything |
| **Continuing development** (engineer) | Engineer track — sections **2.x** | Understand the architecture, deploy changes safely, set up a new server |
| **Looking something up** | Reference — sections **3.x** | Tools/costs/access, why decisions were made, troubleshooting, the access sheet |

You do **not** need to read this top to bottom. Use the Table of Contents (auto-generated
at the top of the built document) or Ctrl-F.

## In an emergency

> **To stop ALL trading immediately:** in Discord, type **`/panic`** (Roy is the
> authorized user; another authorized user must be configured to use it). This halts every
> bot, cancels open orders, and closes positions. It is safe to use — the system is built
> to be stopped.
>
> **If something looks wrong but isn't urgent:** check the **#alerts Discord channel** and
> the **Grafana dashboard** first (see *Operator Basics*). Most problems announce
> themselves there. Nothing here trades real money without a deliberate switch being
> flipped, so when in doubt, **stop it and ask** — a halted bot loses nothing.

## What this is NOT

- It is **not** investment advice or a guaranteed money-maker. It is an experiment to
  measure whether a trading edge exists *before* risking real money.
- It does **not** trade your savings. Real-money trading is gated behind explicit flags
  that are currently **off** (see *Changing Safely*).

## Contacts & escalation

_Fill this in and keep it current:_

- **Owner:** Roy — _phone / email: _______________________
- **If Roy is unavailable, the technical contact is:** _______________________
- **Server provider:** Hetzner (account login on the Access Sheet)
- **Where the passwords live:** the printed **Access Sheet** (section 3.4) + your family
  password sheets.
