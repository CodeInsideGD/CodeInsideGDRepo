#!/bin/bash

# активируем опцию, которая прерывает выполнение скрипта, если любая команда завершается с ненулевым статусом
set -e

# проверим, запущен ли скрипт от пользователя root
if [[ "${UID}" -ne 0 ]]; then
  echo -e "You need to run this script as root!\nPlease apply 'sudo su root' and add your host-key to /root/.ssh/authorized_keys before run this script!"
  exit 1
fi

# проверим, загружены ли открытые ssh-ключи у пользователя localhost
if [ ! -f ~/.ssh/authorized_keys ]; then
  echo -e "\n====================\nFile /localhost/.ssh/authorized_keys not found!\n====================\n"
  exit 1
else
  if [ ! -s ~/.ssh/authorized_keys ]; then
    echo -e "\n====================\nFile /localhost/.ssh/authorized_keys is empty!\n====================\n"
    exit 1
  fi
fi

# функция, которая проверяет наличие пакета в системе и в случае его отсутствия выполняет установку
command_check() {
  if ! command -v "$1" &>/dev/null; then
    echo -e "\n====================\n$2 could not be found!\nInstalling...\n====================\n"
    dnf install -y "$3"
    echo -e "\nDONE\n"
  fi
}

#функция, которая запрашивает имя нового пользователя и проверяет его на наличие в системе
username_request() {
 while true; do
   read -r -p $'\n'"new username: " username
   if id "$username" >/dev/null 2>&1; then
     echo -e "\nUser $username exists!\n"
   else
     break
   fi
 done
}

# функция, которая проверяет наличие правила в iptables и в случае отсутствия применяет его
iptables_add() {
  if ! iptables -C "$@" &>/dev/null; then
    iptables -A "$@"
  fi
}

# настроим часовой пояс
echo -e "\n====================\nSetting timezone\n===================="
timedatectl set-timezone Europe/Moscow
systemctl restart timedatex.service
timedatectl
echo -e "\nDONE\n"

# установим все необходимые пакеты используя функцию command_check
dnf update
command_check wget "Wget" wget
command_check iptables "Iptables" iptables
command_check iptables-services "iptables-services" iptables-services
command_check openssl "Openssl" openssl
command_check update-ca-certificates "Ca-certificates" ca-certificates

# проверим наличие конфигурационного файла ssh
if [ ! -f /etc/ssh/sshd_config ]; then
  echo -e "\n====================\nFile /etc/ssh/sshd_config not found!\n====================\n"
  exit 1
fi

# проверим наличие конфигурационного файла grub
if [ ! -f /etc/default/grub ]; then
  echo -e "\n====================\nFile /etc/default/grub not found!\n====================\n"
  exit 1
fi

# создадим нового пользователя
echo -e "\n====================\nNew user config\n===================="

while true; do
  read -r -n 1 -p "Continue or Skip? (c|s) " cs
  case $cs in
  [Cc]*)
    # запросим имя пользователя используя функцию username_request (функция создаст глобальную переменную "username")
    username_request

    # запросим пароль для нового пользователя
    read -r -p "new password: " -s password

    # создадим нового пользователя и перенесем ssh-ключи
    useradd -p "$(openssl passwd -1 "$password")" "$username" -s /bin/bash -m -G sudo
    cp -r /root/.ssh/ /home/"$username"/ && chown -R "$username":"$username" /home/"$username"/.ssh/
    echo -e "\n\nDONE\n"

    # выполним корректировку prompt statement
    echo -e "\n====================\nEdit prompt statement for this user?\n===================="

    while true; do
      read -r -n 1 -p "Continue or Skip? (c|s) " cs
      case $cs in
      [Cc]*)
        # запросим имя vm
        read -r -p $'\n'"vm name: " vm_name

        # выполним корректировку prompt statement
        echo "PS1='\${debian_chroot:+(\$debian_chroot)}\\u@$vm_name:\\w\\\$ '" >>/home/"$username"/.bashrc

        echo -e "\n\nDONE\n"
        break
        ;;
      [Ss]*)
        echo -e "\n"
        break
        ;;
      *) echo -e "\nPlease answer C or S!\n" ;;
      esac
    done

    break
    ;;
  [Ss]*)
    echo -e "\n"
    break
    ;;
  *) echo -e "\nPlease answer C or S!\n" ;;
  esac
done


# настроим ssh
echo -e "\n====================\nEdit sshd_config file\n===================="

while true; do 
  read -r -n 1 -p "Continue or Skip? (c|s) " cs
  case $cs in
  [Cc]*)
    sed -i 's/#\?\(Port\s*\).*$/\1 22/' /etc/ssh/sshd_config
    sed -i 's/#\?\(PermitRootLogin\s*\).*$/\1 no/' /etc/ssh/sshd_config
    sed -i 's/#\?\(PubkeyAuthentication\s*\).*$/\1 yes/' /etc/ssh/sshd_config
    sed -i 's/#\?\(PermitEmptyPasswords\s*\).*$/\1 no/' /etc/ssh/sshd_config
    sed -i 's/#\?\(PasswordAuthentication\s*\).*$/\1 no/' /etc/ssh/sshd_config
    echo -e "\n\n"
    systemctl restart sshd
    echo -e "\nDONE\n"
    break
    ;;

  [Ss]*)
    echo -e "\n"
    break
    ;;
  *) echo -e "\nPlease answer C or S!\n" ;;
  esac
done


# настроим iptables
echo -e "\n====================\nIptables config\n===================="
while true; do
  read -r -n 1 -p "Current ssh session may drop! To continue you have to relogin to this host via 22 ssh-port and run this script again. Are you ready? (y|n) " yn
  case $yn in
  [Yy]*) #---DNS---
    iptables_add OUTPUT -p tcp --dport 53 -j ACCEPT -m comment --comment dns
    iptables_add OUTPUT -p udp --dport 53 -j ACCEPT -m comment --comment dns
    #---NTP---
    iptables_add OUTPUT -p udp --dport 123 -j ACCEPT -m comment --comment ntp
    #---ICMP---
    iptables_add OUTPUT -p icmp -m state --state NEW,ESTABLISHED,RELATED -j ACCEPT
    iptables_add INPUT -p icmp -m state --state NEW,ESTABLISHED,RELATED -j ACCEPT
    #---loopback---
    iptables_add OUTPUT -o lo -j ACCEPT
    iptables_add INPUT -i lo -j ACCEPT
    #---Input-SSH---
    iptables_add INPUT -p tcp --dport 22 -j ACCEPT -m comment --comment ssh
    #---Output-HTTP---
    iptables_add OUTPUT -p tcp -m multiport --dports 443,80 -j ACCEPT
    #---ESTABLISHED---
    iptables_add INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    iptables_add OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    #---INVALID---
    iptables_add OUTPUT -m state --state INVALID -j DROP
    iptables_add INPUT -m state --state INVALID -j DROP
    #---Defaul-Drop---
    iptables -P OUTPUT DROP
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    # save iptables config
    echo -e "\n====================\nSaving iptables config\n====================\n"
    iptables-save
    echo -e "DONE\n"
    break
    ;;
  [Nn]*)
    echo -e "\n"
    exit
    ;;
  *) echo -e "\nPlease answer Y or N!\n" ;;
  esac
done

echo -e "\nOK\n"
exit 0

